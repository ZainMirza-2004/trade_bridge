"""Execution adapters. Paper and live trading share the engine; only the adapter differs."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.decimal_utils import ZERO, round_down, to_decimal
from app.core.enums import OrderStatus
from app.exchanges.revolut_x import (
    BadRequestError,
    DuplicateClientOrderIdError,
    NetworkError,
    NotFoundError,
    PairInfo,
    RateLimitError,
    RevolutXClient,
    RevolutXError,
)

logger = logging.getLogger(__name__)


class OrderUncertainError(Exception):
    """Submission may or may not have reached the exchange. Must be reconciled before any new order."""

    def __init__(self, client_order_id: str, message: str):
        super().__init__(message)
        self.client_order_id = client_order_id


class ExecutionError(Exception):
    """The order was definitely not placed (validation, connectivity before send, insufficient funds)."""


@dataclass
class OrderResult:
    client_order_id: str
    status: OrderStatus
    exchange_order_id: str | None = None
    filled_quantity: Decimal = ZERO  # gross base quantity executed
    average_fill_price: Decimal | None = None
    fee: Decimal = ZERO  # in fee_currency
    fee_currency: str | None = None
    fee_quote: Decimal = ZERO  # fee expressed in quote currency
    net_base_quantity: Decimal = ZERO  # base actually received (buy) or delivered (sell)
    reject_reason: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class ExecutionAdapter(ABC):
    mode: str

    @abstractmethod
    async def buy(self, symbol: str, amount: Decimal, client_order_id: str) -> OrderResult:
        """Market buy spending ``amount`` of the quote currency."""

    @abstractmethod
    async def sell(self, symbol: str, quantity: Decimal, client_order_id: str) -> OrderResult:
        """Market sell ``quantity`` of the base currency."""

    @abstractmethod
    async def get_balance(self) -> dict[str, Decimal]:
        """Available balance per currency."""

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderResult:
        """Current state of an order by exchange order id."""

    async def find_order(self, client_order_id: str, symbol: str) -> OrderResult | None:
        return None

    async def get_active_orders(self, symbol: str) -> list[dict]:
        """Raw open orders on the venue for ``symbol``."""
        return []

    async def get_recent_orders(self, symbol: str, since_ms: int) -> list[dict]:
        """Raw historical (terminal) orders on the venue for ``symbol`` since ``since_ms``."""
        return []


def parse_exchange_order(raw: dict, pair: PairInfo | None) -> OrderResult:
    status_raw = (raw.get("status") or raw.get("state") or "").lower()
    try:
        status = OrderStatus(status_raw)
    except ValueError:
        status = OrderStatus.NEW
    filled = to_decimal(raw.get("filled_quantity"), ZERO)
    avg = to_decimal(raw.get("average_fill_price"))
    if avg is None and filled > 0:
        filled_amount = to_decimal(raw.get("filled_amount"))
        avg = filled_amount / filled if filled_amount else to_decimal(raw.get("price"))
    fee = to_decimal(raw.get("total_fee"), ZERO)
    fee_ccy = raw.get("fee_currency")
    side = (raw.get("side") or "").lower()
    fee_quote = ZERO
    net_base = filled
    if fee and fee_ccy and pair is not None:
        if fee_ccy == pair.base:
            fee_quote = fee * (avg or ZERO)
            net_base = filled - fee if side == "buy" else filled
        else:
            fee_quote = fee
    elif fee:
        fee_quote = fee
    return OrderResult(
        client_order_id=raw.get("client_order_id", ""),
        exchange_order_id=raw.get("id") or raw.get("venue_order_id"),
        status=status,
        filled_quantity=filled,
        average_fill_price=avg,
        fee=fee,
        fee_currency=fee_ccy,
        fee_quote=fee_quote,
        net_base_quantity=net_base,
        reject_reason=raw.get("reject_reason"),
        raw=raw,
    )


class RevolutXExecutionAdapter(ExecutionAdapter):
    mode = "LIVE"

    def __init__(
        self,
        client: RevolutXClient,
        pair_provider: Callable[[str], Awaitable[PairInfo]],
        poll_interval: float = 0.5,
        status_timeout: float = 15.0,
    ):
        self.client = client
        self.pair_provider = pair_provider
        self.poll_interval = poll_interval
        self.status_timeout = status_timeout

    async def buy(self, symbol: str, amount: Decimal, client_order_id: str) -> OrderResult:
        pair = await self.pair_provider(symbol)
        quote_size = round_down(amount, pair.quote_step)
        if quote_size < pair.min_order_size_quote:
            raise ExecutionError(f"Order value {quote_size} below minimum {pair.min_order_size_quote} {pair.quote}")
        request = self.client.build_order_request(symbol, "buy", "market", client_order_id, quote_size=quote_size)
        return await self._submit(request, pair)

    async def sell(self, symbol: str, quantity: Decimal, client_order_id: str) -> OrderResult:
        pair = await self.pair_provider(symbol)
        base_size = round_down(quantity, pair.base_step)
        if base_size <= 0 or base_size < pair.min_order_size:
            raise ExecutionError(f"Quantity {base_size} below minimum {pair.min_order_size} {pair.base}")
        request = self.client.build_order_request(symbol, "sell", "market", client_order_id, base_size=base_size)
        return await self._submit(request, pair)

    async def _submit(self, request: dict, pair: PairInfo) -> OrderResult:
        coid = request["client_order_id"]
        try:
            placed = await self.client.place_order(request)
        except DuplicateClientOrderIdError:
            found = await self.find_order(coid, pair.symbol)
            if found is None:
                raise OrderUncertainError(coid, "Exchange reports duplicate client_order_id but order not found yet")
            return await self._await_terminal(found, pair)
        except NetworkError as exc:
            if exc.may_have_reached_server:
                raise OrderUncertainError(coid, exc.message) from exc
            raise ExecutionError(exc.message) from exc
        except RateLimitError as exc:
            raise ExecutionError(f"Rate limited by Revolut X: {exc.message}") from exc
        except BadRequestError as exc:
            return OrderResult(client_order_id=coid, status=OrderStatus.REJECTED, reject_reason=exc.message)
        except RevolutXError as exc:
            if exc.status is not None and exc.status >= 500:
                # A 5xx on POST may still have created the order.
                raise OrderUncertainError(coid, exc.message) from exc
            raise ExecutionError(exc.message) from exc

        result = OrderResult(
            client_order_id=coid,
            exchange_order_id=placed.get("venue_order_id"),
            status=OrderStatus((placed.get("state") or "pending_new").lower()),
        )
        return await self._await_terminal(result, pair)

    async def _await_terminal(self, result: OrderResult, pair: PairInfo) -> OrderResult:
        """Never trust acceptance: poll the order until it reaches a terminal state."""
        if result.exchange_order_id is None:
            return result
        deadline = time.monotonic() + self.status_timeout
        latest = result
        while True:
            try:
                raw = await self.client.get_order(result.exchange_order_id)
                latest = parse_exchange_order(raw, pair)
                latest.client_order_id = latest.client_order_id or result.client_order_id
            except NotFoundError:
                pass
            except (NetworkError, RevolutXError) as exc:
                logger.warning("Order status poll failed for %s: %s", result.exchange_order_id, exc)
            if latest.is_terminal or time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.poll_interval)
        if latest.is_terminal and latest.filled_quantity > 0 and latest.average_fill_price is None:
            await self._fill_price_from_fills(latest)
        return latest

    async def _fill_price_from_fills(self, result: OrderResult) -> None:
        fills = await self.client.get_order_fills(result.exchange_order_id)  # type: ignore[arg-type]
        qty = sum((to_decimal(f.get("q"), ZERO) for f in fills), ZERO)
        if qty > 0:
            notional = sum((to_decimal(f.get("q"), ZERO) * to_decimal(f.get("p"), ZERO) for f in fills), ZERO)
            result.average_fill_price = notional / qty

    async def get_balance(self) -> dict[str, Decimal]:
        balances = await self.client.get_balances()
        return {b["currency"]: to_decimal(b.get("available"), ZERO) for b in balances}

    async def get_order_status(self, order_id: str) -> OrderResult:
        raw = await self.client.get_order(order_id)
        symbol = (raw.get("symbol") or "").replace("/", "-")
        pair = await self.pair_provider(symbol) if symbol else None
        return parse_exchange_order(raw, pair)

    async def find_order(self, client_order_id: str, symbol: str) -> OrderResult | None:
        raw = await self.client.find_order_by_client_id(client_order_id, symbol)
        if raw is None:
            return None
        pair = await self.pair_provider(symbol)
        if raw.get("id"):
            try:
                raw = await self.client.get_order(raw["id"])  # detail view carries fees
            except RevolutXError:
                pass
        return parse_exchange_order(raw, pair)

    async def get_active_orders(self, symbol: str) -> list[dict]:
        return await self.client.get_active_orders([symbol])

    async def get_recent_orders(self, symbol: str, since_ms: int) -> list[dict]:
        return await self.client.get_historical_orders([symbol], start_date=since_ms)
