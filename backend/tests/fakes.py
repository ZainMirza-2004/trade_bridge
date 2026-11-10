"""Test doubles shared by engine tests. Nothing here talks to the network."""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal

from app.core.config import Settings
from app.core.decimal_utils import ZERO
from app.core.enums import OrderStatus
from app.exchanges.revolut_x import PairInfo
from app.indicators.base import StandardSignal
from app.trading.execution import ExecutionAdapter, OrderResult

PAIR = PairInfo(
    symbol="BTC-GBP",
    base="BTC",
    quote="GBP",
    base_step=Decimal("0.00000001"),
    quote_step=Decimal("0.01"),
    min_order_size=Decimal("0.00001"),
    max_order_size=Decimal("1000"),
    min_order_size_quote=Decimal("1"),
    max_order_size_quote=None,
    status="active",
)


class FakeBroadcaster:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    async def broadcast(self, event_type, data):
        self.events.append((event_type, data))

    def types(self):
        return [e for e, _ in self.events]


class FakeMarket:
    def __init__(self, bid="50000", ask="50010"):
        self.bid, self.ask = Decimal(bid), Decimal(ask)
        self.pairs = {PAIR.symbol: PAIR}
        self.symbol, self.timeframe = "BTC-GBP", "1m"
        self.ticker = {"symbol": "BTC-GBP", "bid": bid, "ask": ask}
        self.stale = False
        self.last_error = None
        self.last_ticker_ok = time.time()
        self.signals = []
        self.on_signal = None

    async def load_pairs(self, force=False):
        return self.pairs

    async def get_pair(self, symbol):
        if symbol not in self.pairs:
            raise ValueError(f"Unknown trading pair {symbol}")
        return self.pairs[symbol]

    async def get_quote(self, symbol):
        return self.bid, self.ask

    def set_price(self, bid, ask=None):
        self.bid = Decimal(str(bid))
        self.ask = Decimal(str(ask if ask is not None else bid))
        self.ticker = {"symbol": "BTC-GBP", "bid": str(self.bid), "ask": str(self.ask)}

    def is_stale(self):
        return self.stale

    def data_status(self):
        return "STALE" if self.stale else "LIVE"

    async def configure(self, symbol, timeframe, indicator, parameters):
        self.symbol, self.timeframe = symbol, timeframe


class ScriptedAdapter(ExecutionAdapter):
    """LIVE-style adapter whose responses are scripted per call."""

    mode = "LIVE"

    def __init__(self, balances=None):
        self.balances = {k: Decimal(str(v)) for k, v in (balances or {"GBP": 1000}).items()}
        self.results: deque = deque()
        self.calls: list[tuple] = []
        self.known: dict[str, OrderResult] = {}
        self.active_orders: list[dict] = []
        self.recent_orders: list[dict] = []

    def script(self, item):
        self.results.append(item)

    async def _next(self, side, symbol, size, coid):
        self.calls.append((side, symbol, size, coid))
        item = self.results.popleft()
        if isinstance(item, Exception):
            raise item
        result = item(coid) if callable(item) else item
        result.client_order_id = coid
        self.known[coid] = result
        return result

    async def buy(self, symbol, amount, client_order_id):
        return await self._next("buy", symbol, amount, client_order_id)

    async def sell(self, symbol, quantity, client_order_id):
        return await self._next("sell", symbol, quantity, client_order_id)

    async def get_balance(self):
        return dict(self.balances)

    async def get_order_status(self, order_id):
        for r in self.known.values():
            if r.exchange_order_id == order_id:
                return r
        raise KeyError(order_id)

    async def find_order(self, client_order_id, symbol):
        return self.known.get(client_order_id)

    async def get_active_orders(self, symbol):
        return list(self.active_orders)

    async def get_recent_orders(self, symbol, since_ms):
        return list(self.recent_orders)


def filled(side, qty, price, fee="0", status=OrderStatus.FILLED, oid="ex-1"):
    qty, price, fee = Decimal(str(qty)), Decimal(str(price)), Decimal(str(fee))
    return OrderResult(
        client_order_id="",
        status=status,
        exchange_order_id=oid,
        filled_quantity=qty,
        average_fill_price=price if qty > 0 else None,
        fee=fee,
        fee_currency="GBP",
        fee_quote=fee,
        net_base_quantity=qty,
    )


def make_signal(signal_type, candle_ts=None, price=50000.0, timeframe="1m"):
    if candle_ts is None:
        candle_ts = (int(time.time() * 1000) // 60_000 - 1) * 60_000
    return StandardSignal("ut_bot", "BTC-GBP", timeframe, signal_type, candle_ts, price, True, {"key_value": 1.0})


def make_settings(**overrides):
    base = dict(
        revolut_x_api_key="",
        revolut_x_private_key_path="",
        paper_starting_balance=Decimal("10000"),
        paper_fee_rate=Decimal("0.001"),
        stale_data_seconds=30,
        live_connection_max_age_seconds=600,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)
