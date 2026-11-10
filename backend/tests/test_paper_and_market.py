import time
from decimal import Decimal

import pytest

from app.core.enums import OrderStatus
from app.services.market_data import MarketDataService
from app.trading.paper import PaperExecutionAdapter
from tests.fakes import PAIR, FakeBroadcaster, FakeMarket, make_settings


async def make_paper(db, bal="1000"):
    m = FakeMarket(bid="100", ask="101")
    p = PaperExecutionAdapter(db, m.get_pair, m.get_quote, Decimal("0.001"), Decimal(bal), "GBP")
    await p.ensure_initialised("GBP")
    return p


async def test_paper_buy_and_sell_fees(db):
    p = await make_paper(db)
    r = await p.buy("BTC-GBP", Decimal("101.101"), "a")
    assert r.status == OrderStatus.FILLED and r.filled_quantity == Decimal("1")  # 101.101 / (101*1.001)
    assert r.fee == Decimal("0.101") and r.average_fill_price == Decimal(101)
    bal = await p.get_balance()
    assert bal["GBP"] == Decimal("1000") - Decimal("101.101") and bal["BTC"] == Decimal(1)
    s = await p.sell("BTC-GBP", Decimal(1), "b")
    assert s.average_fill_price == Decimal(100) and s.fee == Decimal("0.1")
    assert (await p.get_balance())["GBP"] == Decimal("1000") - Decimal("101.101") + Decimal("99.9")


async def test_paper_rejects_insufficient_funds_and_never_goes_negative(db):
    p = await make_paper(db, bal="50")
    r = await p.buy("BTC-GBP", Decimal("60"), "a")
    assert r.status == OrderStatus.REJECTED
    s = await p.sell("BTC-GBP", Decimal("1"), "b")
    assert s.status == OrderStatus.REJECTED
    assert (await p.get_balance())["GBP"] == Decimal(50)


async def test_paper_quantity_rounds_down_to_step(db):
    p = await make_paper(db)
    r = await p.buy("BTC-GBP", Decimal("10"), "a")
    assert r.filled_quantity == r.filled_quantity.quantize(PAIR.base_step)
    assert r.filled_quantity * 101 * Decimal("1.001") <= 10


class FakeCandleClient:
    def __init__(self):
        self.candles = []

    async def get_public_candles(self, symbol, interval, since=None, until=None):
        return [c for c in self.candles if (since is None or c["start"] >= since)]

    async def get_public_pairs(self):
        return {PAIR.symbol: PAIR}


def bar(t, o, h, l, c):
    return {"start": t, "open": str(o), "high": str(h), "low": str(l), "close": str(c), "volume": "1"}


async def test_market_service_only_emits_signals_for_newly_closed_candles():
    now = [1_800_000_000.0]
    client = FakeCandleClient()
    step = 60_000
    t0 = int(now[0] * 1000) // step * step - 40 * step
    price = 100.0
    for i in range(40):  # steady uptrend history
        price += 1
        client.candles.append(bar(t0 + i * step, price - 1, price + 0.5, price - 1.5, price))
    svc = MarketDataService(client, make_settings(), FakeBroadcaster(), clock=lambda: now[0])
    received = []

    async def handler(sig):
        received.append(sig)

    svc.on_signal = handler
    await svc.configure("BTC-GBP", "1m", "ut_bot", {"key_value": 1, "atr_period": 5})
    assert received == [] and len(svc.closed) == 39  # last candle still forming
    hist_signals = len(svc.signals)

    # A crash candle forms: not closed yet -> no signal even though it crosses the stop.
    crash_t = t0 + 40 * step
    client.candles.append(bar(crash_t, price, price, price - 30, price - 30))
    now[0] = (crash_t + 30_000) / 1000
    await svc.poll_candles()
    assert received == []

    now[0] = (crash_t + step + 5_000) / 1000
    await svc.poll_candles()
    assert [s.signal_type for s in received] == ["SELL"] and received[0].candle_ts == crash_t
    await svc.poll_candles()  # re-polling the same data must not duplicate
    assert len(received) == 1 and len(svc.signals) == hist_signals + 1


def test_staleness():
    now = [1000.0]
    svc = MarketDataService(FakeCandleClient(), make_settings(stale_data_seconds=10), FakeBroadcaster(), clock=lambda: now[0])
    assert svc.is_stale()
    svc.last_ticker_ok = svc.last_candles_ok = now[0]
    assert not svc.is_stale()
    now[0] += 11
    assert svc.is_stale()
