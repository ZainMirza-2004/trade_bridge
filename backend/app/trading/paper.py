"""Paper execution: real Revolut X prices, simulated fills, persisted balances."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.decimal_utils import ZERO, round_down
from app.core.enums import OrderStatus
from app.database.models import PaperBalance
from app.database.session import Database
from app.exchanges.revolut_x import PairInfo
from app.trading.execution import ExecutionAdapter, ExecutionError, OrderResult


class PaperExecutionAdapter(ExecutionAdapter):
    mode = "PAPER"

    def __init__(
        self,
        db: Database,
        pair_provider: Callable[[str], Awaitable[PairInfo]],
        quote_provider: Callable[[str], Awaitable[tuple[Decimal, Decimal]]],  # (bid, ask)
        fee_rate: Decimal,
        starting_balance: Decimal,
        starting_currency: str = "GBP",
    ):
        self.db = db
        self.pair_provider = pair_provider
        self.quote_provider = quote_provider
        self.fee_rate = fee_rate
        self.starting_balance = starting_balance
        self.starting_currency = starting_currency
        self._orders: dict[str, OrderResult] = {}

    async def ensure_initialised(self, quote_currency: str | None = None) -> None:
        async with self.db.transaction() as s:
            if (await s.execute(select(PaperBalance).limit(1))).first() is None:
                s.add(PaperBalance(currency=quote_currency or self.starting_currency, available=self.starting_balance))

    async def reset(self, starting_balance: Decimal, quote_currency: str) -> None:
        async with self.db.transaction() as s:
            await s.execute(delete(PaperBalance))
            s.add(PaperBalance(currency=quote_currency, available=starting_balance))
        self.starting_balance = starting_balance
        self.starting_currency = quote_currency

    async def get_balance(self) -> dict[str, Decimal]:
        async with self.db.session() as s:
            rows = (await s.execute(select(PaperBalance))).scalars().all()
            return {r.currency: r.available for r in rows}

    async def _adjust(self, changes: dict[str, Decimal]) -> None:
        async with self.db.transaction() as s:
            for ccy, delta in changes.items():
                row = await s.get(PaperBalance, ccy, with_for_update=True)
                if row is None:
                    row = PaperBalance(currency=ccy, available=ZERO)
                    s.add(row)
                new = (row.available or ZERO) + delta
                if new < 0:
                    raise ExecutionError(f"Insufficient paper {ccy} balance")
                row.available = new

    async def buy(self, symbol: str, amount: Decimal, client_order_id: str) -> OrderResult:
        pair = await self.pair_provider(symbol)
        _, ask = await self.quote_provider(symbol)
        if ask <= 0:
            raise ExecutionError("No ask price available")
        balance = (await self.get_balance()).get(pair.quote, ZERO)
        if amount > balance:
            return self._record(OrderResult(client_order_id, OrderStatus.REJECTED, reject_reason=f"Insufficient {pair.quote} balance"))
        if amount < pair.min_order_size_quote:
            return self._record(OrderResult(client_order_id, OrderStatus.REJECTED, reject_reason="Below minimum order size"))
        qty = round_down(amount / (ask * (1 + self.fee_rate)), pair.base_step)
        if qty <= 0:
            return self._record(OrderResult(client_order_id, OrderStatus.REJECTED, reject_reason="Quantity rounds to zero"))
        notional = qty * ask
        fee = notional * self.fee_rate
        await self._adjust({pair.quote: -(notional + fee), pair.base: qty})
        return self._record(
            OrderResult(
                client_order_id=client_order_id,
                exchange_order_id=f"paper-{client_order_id}",
                status=OrderStatus.FILLED,
                filled_quantity=qty,
                average_fill_price=ask,
                fee=fee,
                fee_currency=pair.quote,
                fee_quote=fee,
                net_base_quantity=qty,
            )
        )

    async def sell(self, symbol: str, quantity: Decimal, client_order_id: str) -> OrderResult:
        pair = await self.pair_provider(symbol)
        bid, _ = await self.quote_provider(symbol)
        if bid <= 0:
            raise ExecutionError("No bid price available")
        held = (await self.get_balance()).get(pair.base, ZERO)
        qty = round_down(min(quantity, held), pair.base_step)
        if qty <= 0 or qty < pair.min_order_size:
            return self._record(OrderResult(client_order_id, OrderStatus.REJECTED, reject_reason=f"Insufficient {pair.base} balance"))
        proceeds = qty * bid
        fee = proceeds * self.fee_rate
        await self._adjust({pair.base: -qty, pair.quote: proceeds - fee})
        return self._record(
            OrderResult(
                client_order_id=client_order_id,
                exchange_order_id=f"paper-{client_order_id}",
                status=OrderStatus.FILLED,
                filled_quantity=qty,
                average_fill_price=bid,
                fee=fee,
                fee_currency=pair.quote,
                fee_quote=fee,
                net_base_quantity=qty,
            )
        )

    def _record(self, result: OrderResult) -> OrderResult:
        self._orders[result.exchange_order_id or result.client_order_id] = result
        self._orders[result.client_order_id] = result
        return result

    async def get_order_status(self, order_id: str) -> OrderResult:
        if order_id in self._orders:
            return self._orders[order_id]
        raise ExecutionError(f"Unknown paper order {order_id}")

    async def find_order(self, client_order_id: str, symbol: str) -> OrderResult | None:
        return self._orders.get(client_order_id)
