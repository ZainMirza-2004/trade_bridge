import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.enums import ExecutionMode, OrderStatus, SignalStatus, TradeStatus
from app.database.models import AppState, Order, Signal, Trade
from app.trading.engine import LIVE_CONFIRMATION, TradingEngine, TradingError
from app.trading.execution import OrderUncertainError
from app.trading.paper import PaperExecutionAdapter
from tests.conftest import D
from tests.fakes import PAIR, FakeBroadcaster, FakeMarket, ScriptedAdapter, filled, make_signal, make_settings


class FakeLiveClient:
    def __init__(self, ok=True):
        self.ok = ok

    async def test_connection(self):
        return {"balances": 2, "pairs": 1}

    async def get_pairs(self):
        return {PAIR.symbol: PAIR}


async def build(db, live: ScriptedAdapter | None = None, market=None):
    settings = make_settings()
    market = market or FakeMarket()
    bc = FakeBroadcaster()
    paper = PaperExecutionAdapter(db, market.get_pair, market.get_quote, settings.paper_fee_rate, settings.paper_starting_balance, "GBP")
    engine = TradingEngine(db, settings, market, bc, paper, live, FakeLiveClient() if live else None)
    await engine.startup()
    await engine.configure(symbol="BTC-GBP", timeframe="1m", position_size_value=Decimal("1000"))
    return engine


async def go_live(engine):
    await engine.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)
    await engine.start()


async def count(db, model, *where):
    async with db.session() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def all_rows(db, model):
    async with db.session() as s:
        return (await s.execute(select(model).order_by(model.id))).scalars().all()


# ------------------------------------------------------------------ paper: full round trip


async def test_buy_opens_and_sell_closes_position_with_fees_and_pnl(db):
    e = await build(db)
    await e.start()
    e.market.set_price(50000, 50000)
    await e.on_signal(make_signal("BUY"))

    [trade] = await all_rows(db, Trade)
    assert trade.status == TradeStatus.OPEN and trade.mode == "PAPER"
    # qty = floor(1000 / (50000*1.001), 1e-8)
    qty = D("0.01998001")
    assert trade.quantity == qty and trade.entry_price == D(50000)
    entry_fee = qty * 50000 * D("0.001")
    assert trade.entry_cost == qty * 50000 + entry_fee
    bal = await e.paper.get_balance()
    assert bal["BTC"] == qty and bal["GBP"] == D(10000) - qty * 50000 - entry_fee

    e.market.set_price(51000, 51000)
    await e.on_signal(make_signal("SELL", candle_ts=make_signal("BUY").candle_ts + 60_000))
    [trade] = await all_rows(db, Trade)
    exit_fee = qty * 51000 * D("0.001")
    assert trade.status == TradeStatus.CLOSED and trade.open_quantity == 0
    assert trade.exit_price == D(51000)
    assert trade.realized_pnl == (qty * 51000 - exit_fee) - (qty * 50000 + entry_fee)
    assert trade.fees == entry_fee + exit_fee
    assert (await e.paper.get_balance())["GBP"] == D(10000) + trade.realized_pnl
    perf = await e.performance()
    assert perf["total_trades"] == 1 and perf["winning_trades"] == 1
    assert {"trade_opened", "trade_closed", "order_update", "balance_update"} <= set(e.broadcaster.types())


async def test_duplicate_signal_is_executed_once(db):
    e = await build(db)
    await e.start()
    sig = make_signal("BUY")
    await e.on_signal(sig)
    await e.on_signal(sig)
    assert await count(db, Order) == 1
    assert await count(db, Signal) == 1


async def test_sell_without_position_does_nothing(db):
    e = await build(db)
    await e.start()
    await e.on_signal(make_signal("SELL"))
    assert await count(db, Order) == 0
    [sig] = await all_rows(db, Signal)
    assert sig.execution_status == SignalStatus.SKIPPED and sig.reason == "No open position"


async def test_second_buy_while_open_is_skipped(db):
    e = await build(db)
    await e.start()
    t0 = make_signal("BUY").candle_ts
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 60_000))
    await e.on_signal(make_signal("BUY", candle_ts=t0))
    assert await count(db, Order) == 1
    assert await count(db, Trade) == 1


async def test_insufficient_balance_skips_buy(db):
    e = await build(db)
    await e.configure(position_size_value=Decimal("20000"))
    await e.start()
    await e.on_signal(make_signal("BUY"))
    assert await count(db, Order) == 0
    [sig] = await all_rows(db, Signal)
    assert "Insufficient balance" in sig.reason


@pytest.mark.parametrize("state", ["stopped", "paused", "emergency", "stale"])
async def test_signals_skipped_when_not_allowed(db, state):
    e = await build(db)
    await e.start()
    if state == "stopped":
        await e.stop()
    elif state == "paused":
        await e.pause()
    elif state == "emergency":
        await e.trigger_emergency_stop()
    else:
        e.market.stale = True
    await e.on_signal(make_signal("BUY"))
    assert await count(db, Order) == 0
    [sig] = await all_rows(db, Signal)
    assert sig.execution_status == SignalStatus.SKIPPED


async def test_emergency_stop_blocks_start_until_reset_and_persists(db):
    e = await build(db)
    await e.start()
    await e.trigger_emergency_stop()
    assert not e.running
    with pytest.raises(TradingError):
        await e.start()
    e2 = await build(db)  # simulated restart on the same database
    assert e2.emergency_stop and e2.mode == ExecutionMode.PAPER and not e2.running
    await e2.reset_emergency_stop()
    await e2.start()
    assert e2.running


async def test_old_signal_not_executed(db):
    e = await build(db)
    await e.start()
    old = make_signal("BUY", candle_ts=int(time.time() * 1000) - 3600_000)
    await e.on_signal(old)
    assert await count(db, Order) == 0


async def test_percent_sizing_capped_by_max_position_size(db):
    e = await build(db)
    await e.configure(position_size_type="percent", position_size_value=Decimal("50"))
    await e.configure_risk(max_position_size=Decimal("200"))
    await e.start()
    await e.on_signal(make_signal("BUY"))
    [order] = await all_rows(db, Order)
    assert order.requested_quote_amount == D(200)


async def test_max_daily_loss_blocks_new_entries(db):
    e = await build(db)
    await e.configure_risk(max_daily_loss=Decimal("5"))
    await e.start()
    t0 = make_signal("BUY").candle_ts
    e.market.set_price(50000)
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 120_000))
    e.market.set_price(49000)
    await e.on_signal(make_signal("SELL", candle_ts=t0 - 60_000))
    assert (await e.realized_pnl_today()) < -5
    await e.on_signal(make_signal("BUY", candle_ts=t0))
    sigs = await all_rows(db, Signal)
    assert sigs[-1].execution_status == SignalStatus.SKIPPED and "daily loss" in sigs[-1].reason


async def test_restart_recovers_open_position_and_can_close_it(db):
    e = await build(db)
    await e.start()
    t0 = make_signal("BUY").candle_ts
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 60_000))
    e2 = await build(db)
    assert len(await e2.trades(status="OPEN")) == 1
    await e2.start()
    await e2.on_signal(make_signal("SELL", candle_ts=t0))
    assert len(await e2.trades(status="OPEN")) == 0


async def test_manual_close(db):
    e = await build(db)
    await e.start()
    await e.on_signal(make_signal("BUY"))
    await e.stop()
    [trade] = await all_rows(db, Trade)
    await e.close_trade(trade.id)
    [trade] = await all_rows(db, Trade)
    assert trade.status == TradeStatus.CLOSED


# ------------------------------------------------------------------ live adapter behaviour (scripted, no network)


async def test_live_mode_requires_confirmation_and_credentials(db):
    e = await build(db)
    with pytest.raises(TradingError):
        await e.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)  # no credentials
    live = ScriptedAdapter()
    e2 = await build(db, live=live)
    with pytest.raises(TradingError):
        await e2.set_mode(ExecutionMode.LIVE, "yes")
    assert e2.mode == ExecutionMode.PAPER
    await e2.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)
    assert e2.mode == ExecutionMode.LIVE


async def test_live_rejected_order_creates_no_trade(db):
    live = ScriptedAdapter()
    e = await build(db, live=live)
    await go_live(e)
    r = filled("buy", 0, 0, status=OrderStatus.REJECTED)
    r.reject_reason = "Insufficient funds"
    live.script(r)
    await e.on_signal(make_signal("BUY"))
    assert await count(db, Trade) == 0
    [order] = await all_rows(db, Order)
    assert order.status == OrderStatus.REJECTED and order.reject_reason == "Insufficient funds"
    [sig] = await all_rows(db, Signal)
    assert sig.execution_status == SignalStatus.FAILED


async def test_live_buy_uses_actual_fills_and_partial_sell_keeps_position_open(db):
    live = ScriptedAdapter({"GBP": 1000})
    e = await build(db, live=live)
    await e.configure(position_size_value=Decimal("500"))
    await go_live(e)
    t0 = make_signal("BUY").candle_ts
    live.script(filled("buy", "0.01", "49900", fee="0.45", oid="b1"))
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 60_000))
    assert live.calls[0][2] == D(500)
    [trade] = await all_rows(db, Trade)
    assert trade.quantity == D("0.01") and trade.entry_price == D(49900) and trade.entry_cost == D("499.45")

    live.balances["BTC"] = D("0.01")
    # exchange cancels the remainder after a partial fill
    live.script(filled("sell", "0.004", "51000", fee="0.2", status=OrderStatus.CANCELLED, oid="s1"))
    await e.on_signal(make_signal("SELL", candle_ts=t0))
    assert live.calls[1][2] == D("0.01")
    [trade] = await all_rows(db, Trade)
    assert trade.status == TradeStatus.OPEN
    assert trade.open_quantity == D("0.006")
    assert trade.realized_pnl == D("0.004") * 51000 - D("0.2") - D("499.45") * D("0.4")


async def test_live_timeout_blocks_without_duplicate_order_then_reconciles(db):
    live = ScriptedAdapter({"GBP": 1000})
    e = await build(db, live=live)
    await go_live(e)
    t0 = make_signal("BUY").candle_ts
    live.script(OrderUncertainError("x", "No reply from Revolut X: ReadTimeout"))
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 60_000))
    [order] = await all_rows(db, Order)
    assert order.status == OrderStatus.UNKNOWN
    assert e.blocked_reason

    # A new signal must not create another order while the first is unresolved.
    await e.on_signal(make_signal("BUY", candle_ts=t0))
    assert len(live.calls) == 1 and await count(db, Order) == 1

    # Exchange shows the order did fill: reconciliation applies it exactly once.
    r = filled("buy", "0.02", "50000", fee="0.9", oid="late")
    r.client_order_id = order.client_order_id
    live.known[order.client_order_id] = r
    await e.reconcile()
    await e.reconcile()
    assert e.blocked_reason is None
    [order] = await all_rows(db, Order)
    assert order.status == OrderStatus.FILLED and order.applied
    assert await count(db, Trade) == 1


async def test_live_sell_limited_to_exchange_balance(db):
    live = ScriptedAdapter({"GBP": 1000})
    e = await build(db, live=live)
    await go_live(e)
    t0 = make_signal("BUY").candle_ts
    live.script(filled("buy", "0.02", "50000", oid="b"))
    await e.on_signal(make_signal("BUY", candle_ts=t0 - 60_000))
    live.balances["BTC"] = D("0.015")
    live.script(filled("sell", "0.015", "50000", oid="s"))
    await e.on_signal(make_signal("SELL", candle_ts=t0))
    assert live.calls[1][2] == D("0.015")


async def test_start_refused_when_market_data_stale(db):
    e = await build(db)
    e.market.stale = True
    with pytest.raises(TradingError):
        await e.start()


async def test_risk_and_state_persisted(db):
    e = await build(db)
    await e.configure_risk(max_open_positions=2, min_balance=Decimal("50"))
    await e.pause()
    async with db.session() as s:
        st = await s.get(AppState, 1)
    assert st.max_open_positions == 2 and st.min_balance == D(50) and st.paused


# ------------------------------------------------------------------ live startup reconciliation


async def test_live_blocked_by_open_exchange_order(db):
    live = ScriptedAdapter()
    live.active_orders = [{"id": "ex-9", "client_order_id": "manual", "side": "buy", "quantity": "0.1"}]
    e = await build(db, live=live)
    with pytest.raises(TradingError, match="reconciliation failed"):
        await e.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)
    assert e.mode == ExecutionMode.PAPER
    assert e.live_check["ok"] is False and "outside this app" in e.live_check["errors"][0]
    live.active_orders = []
    await e.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)
    assert e.mode == ExecutionMode.LIVE and e.live_check["ok"]


async def test_live_blocked_when_exchange_fill_differs_from_local_record(db):
    live = ScriptedAdapter()
    e = await build(db, live=live)
    await go_live(e)
    live.script(filled("buy", "0.02", 50000, oid="ex-1"))
    await e.on_signal(make_signal("BUY"))
    order = (await all_rows(db, Order))[0]
    await e.stop()
    live.balances["BTC"] = Decimal("0.02")
    live.recent_orders = [{"id": "ex-1", "client_order_id": order.client_order_id, "side": "buy", "filled_quantity": "0.03"}]
    with pytest.raises(TradingError, match="exchange filled 0.03"):
        await e.start()
    assert not e.running
    live.recent_orders = [{"id": "ex-1", "client_order_id": order.client_order_id, "side": "buy", "filled_quantity": "0.02"}]
    await e.start()
    assert e.running


async def test_live_blocked_when_position_not_covered_by_balance(db):
    live = ScriptedAdapter()
    e = await build(db, live=live)
    await go_live(e)
    live.script(filled("buy", "0.02", 50000, oid="ex-1"))
    await e.on_signal(make_signal("BUY"))
    await e.stop()
    live.balances["BTC"] = Decimal("0.001")
    with pytest.raises(TradingError, match="expects 0.02"):
        await e.start()


async def test_untracked_exchange_holdings_are_warnings_not_errors(db):
    live = ScriptedAdapter(balances={"GBP": 1000, "BTC": "0.5"})
    live.recent_orders = [{"id": "ex-7", "client_order_id": "manual-x", "side": "buy", "filled_quantity": "0.5"}]
    e = await build(db, live=live)
    await go_live(e)
    assert e.running and e.live_check["ok"]
    assert len(e.live_check["warnings"]) == 2


async def test_live_blocked_by_unresolved_uncertain_order_on_restart(db):
    live = ScriptedAdapter()
    e = await build(db, live=live)
    await go_live(e)
    live.script(OrderUncertainError("cid", "timeout"))
    await e.on_signal(make_signal("BUY"))
    await e.stop()
    await e.set_mode(ExecutionMode.PAPER)
    e2 = TradingEngine(db, e.settings, e.market, FakeBroadcaster(), e.paper, live, FakeLiveClient())
    await e2.startup()
    assert e2.mode == ExecutionMode.PAPER
    with pytest.raises(TradingError, match="unknown outcome"):
        await e2.set_mode(ExecutionMode.LIVE, LIVE_CONFIRMATION)
    assert len(live.calls) == 1
