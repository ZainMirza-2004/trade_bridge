"""Trading engine: turns confirmed indicator signals into orders, positions and P&L.

The same code path serves PAPER and LIVE; only the ExecutionAdapter differs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.decimal_utils import ZERO, to_decimal
from app.core.enums import (
    ConnectionStatus,
    ExecutionMode,
    OrderSide,
    OrderStatus,
    SignalStatus,
    SignalType,
    TradeStatus,
)
from app.core.timeframes import interval_ms
from app.database.models import AppState, Order, Signal, Trade, TradingConfig, utcnow
from app.database.session import Database
from app.exchanges.revolut_x import RevolutXClient, RevolutXError
from app.indicators.base import StandardSignal
from app.indicators.registry import get_indicator
from app.services.market_data import MarketDataService
from app.trading.execution import ExecutionAdapter, ExecutionError, OrderResult, OrderUncertainError
from app.trading.paper import PaperExecutionAdapter
from app.trading.risk import RiskLimits, check_entry, size_entry
from app.websocket.manager import Broadcaster

logger = logging.getLogger(__name__)

OPEN_ORDER_STATUSES = [
    OrderStatus.CREATED,
    OrderStatus.UNKNOWN,
    OrderStatus.PENDING_NEW,
    OrderStatus.NEW,
    OrderStatus.PARTIALLY_FILLED,
]
UNRESOLVED_GRACE_SECONDS = 120
LIVE_CONFIRMATION = "ENABLE LIVE TRADING"
DEFAULT_CONFIG = dict(indicator="ut_bot", symbol="BTC-GBP", timeframe="1h", position_size_type="fixed", position_size_value=Decimal("100"))


class TradingError(Exception):
    """User-facing refusal (bad state or input)."""


def dec(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f") if v == v.to_integral() else format(v, "f").rstrip("0").rstrip(".")


def ts_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def trade_dict(t: Trade, bid: Decimal | None = None) -> dict[str, Any]:
    unrealized = None
    if t.status == TradeStatus.OPEN and bid is not None and t.quantity > 0:
        unrealized = t.open_quantity * bid - t.entry_cost * t.open_quantity / t.quantity
    return {
        "id": t.id,
        "mode": t.mode,
        "indicator": t.indicator,
        "symbol": t.symbol,
        "side": t.side,
        "status": t.status,
        "entry_price": dec(t.entry_price),
        "exit_price": dec(t.exit_price),
        "quantity": dec(t.quantity),
        "open_quantity": dec(t.open_quantity),
        "entry_cost": dec(t.entry_cost),
        "exit_proceeds": dec(t.exit_proceeds),
        "fees": dec(t.fees),
        "realized_pnl": dec(t.realized_pnl),
        "realized_pnl_pct": dec(t.realized_pnl / t.entry_cost * 100) if t.status == TradeStatus.CLOSED and t.entry_cost else None,
        "unrealized_pnl": dec(unrealized),
        "entry_ts": ts_ms(t.entry_ts),
        "exit_ts": ts_ms(t.exit_ts),
        "entry_signal_ts": ts_ms(t.entry_signal_ts),
        "exit_signal_ts": ts_ms(t.exit_signal_ts),
        "duration_seconds": int((t.exit_ts - t.entry_ts).total_seconds()) if t.exit_ts and t.entry_ts else None,
    }


def order_dict(o: Order) -> dict[str, Any]:
    return {
        "id": o.id,
        "client_order_id": o.client_order_id,
        "exchange_order_id": o.exchange_order_id,
        "signal_id": o.signal_id,
        "mode": o.mode,
        "symbol": o.symbol,
        "side": o.side,
        "status": o.status,
        "requested_quantity": dec(o.requested_quantity),
        "requested_quote_amount": dec(o.requested_quote_amount),
        "filled_quantity": dec(o.filled_quantity),
        "average_fill_price": dec(o.average_fill_price),
        "fees": dec(o.fees),
        "fee_currency": o.fee_currency,
        "reject_reason": o.reject_reason,
        "created_at": ts_ms(o.created_at),
    }


def signal_dict(s: Signal) -> dict[str, Any]:
    return {
        "id": s.id,
        "indicator": s.indicator,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "signal_type": s.signal_type,
        "signal_price": dec(s.signal_price),
        "candle_ts": ts_ms(s.candle_ts),
        "mode": s.mode,
        "execution_status": s.execution_status,
        "reason": s.reason,
        "created_at": ts_ms(s.created_at),
    }


class TradingEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        market: MarketDataService,
        broadcaster: Broadcaster,
        paper: PaperExecutionAdapter,
        live: ExecutionAdapter | None = None,
        live_client: RevolutXClient | None = None,
        clock=time.time,
    ):
        self.db = db
        self.settings = settings
        self.market = market
        self.broadcaster = broadcaster
        self.paper = paper
        self.live = live
        self.live_client = live_client
        self.clock = clock

        self.mode = ExecutionMode.PAPER
        self.running = False
        self.paused = False
        self.emergency_stop = False
        self.limits = RiskLimits()
        self.config: TradingConfig | None = None
        self.blocked_reason: str | None = None
        self.connection_status = ConnectionStatus.DISCONNECTED if live_client is None else ConnectionStatus.CONNECTING
        self.connection_error: str | None = None if live_client else "Revolut X API credentials not configured"
        self.connection_verified_at: float | None = None
        self.reconciliation_warnings: list[str] = []
        self.live_check: dict[str, Any] | None = None
        self.last_skip_reason: str | None = None
        self._lock = asyncio.Lock()

        market.on_signal = self.on_signal
        if hasattr(market, "on_data_status"):
            market.on_data_status = self._on_data_status

    # ================================================================== lifecycle

    @property
    def adapter(self) -> ExecutionAdapter:
        if self.mode == ExecutionMode.LIVE:
            if self.live is None:
                raise TradingError("Live adapter not available")
            return self.live
        return self.paper

    async def startup(self) -> None:
        async with self.db.transaction() as s:
            state = await s.get(AppState, 1)
            if state is None:
                state = AppState(id=1)
                s.add(state)
                await s.flush()
            self._load_state(state)
            cfg = (await s.execute(select(TradingConfig).where(TradingConfig.is_active.is_(True)).order_by(TradingConfig.id.desc()))).scalars().first()
            if cfg is None:
                cfg = TradingConfig(**DEFAULT_CONFIG, parameters=get_indicator("ut_bot").default_settings(), execution_mode="PAPER", is_active=True)
                s.add(cfg)
            cfg.execution_mode = ExecutionMode.PAPER  # always start in paper mode
            await s.flush()
            self.config = cfg
        pairs = await self.market.load_pairs()
        if self.config.symbol not in pairs and pairs:
            await self._update_config(symbol=sorted(pairs)[0])
        await self.market.configure(self.config.symbol, self.config.timeframe, self.config.indicator, self.config.parameters)
        await self.paper.ensure_initialised(self.config.symbol.split("-")[1])
        if self.live_client is not None:
            await self.verify_connection()
        await self.reconcile()

    def _load_state(self, state: AppState) -> None:
        self.paused = state.paused
        self.emergency_stop = state.emergency_stop
        self.limits = RiskLimits(
            max_position_size=state.max_position_size,
            max_daily_loss=state.max_daily_loss,
            max_open_positions=state.max_open_positions,
            min_balance=state.min_balance or ZERO,
        )

    async def _update_state(self, **fields: Any) -> None:
        async with self.db.transaction() as s:
            state = await s.get(AppState, 1)
            for k, v in fields.items():
                setattr(state, k, v)
            await s.flush()
            self._load_state(state)

    async def _update_config(self, **fields: Any) -> None:
        async with self.db.transaction() as s:
            cfg = await s.get(TradingConfig, self.config.id)
            for k, v in fields.items():
                setattr(cfg, k, v)
            await s.flush()
            self.config = cfg

    # ================================================================== connection

    async def verify_connection(self) -> dict[str, Any]:
        if self.live_client is None:
            self.connection_status = ConnectionStatus.DISCONNECTED
            self.connection_error = "Revolut X API credentials not configured"
            raise TradingError(self.connection_error)
        try:
            info = await self.live_client.test_connection()
            pairs = await self.live_client.get_pairs()
        except RevolutXError as exc:
            self.connection_status = ConnectionStatus.AUTH_FAILED if exc.status in (401, 403) else ConnectionStatus.API_UNAVAILABLE
            hint = ""
            if "whitelisted ip" in str(exc).lower() or exc.status == 403:
                hint = " (this machine's public IP is not allow-listed on the API key; see docs/REVOLUT_X_SETUP.md)"
            elif exc.status == 401:
                hint = " (check REVOLUT_X_API_KEY and that the private key matches the public key registered on the API key)"
            elif exc.status == 409:
                hint = " (request timestamp rejected; sync this machine's clock with NTP)"
            self.connection_error = f"{exc}{hint}"
            self.connection_verified_at = None
            await self.broadcast_status()
            raise TradingError(f"Revolut X connection failed: {exc}") from exc
        self.connection_status = ConnectionStatus.CONNECTED
        self.connection_error = None
        self.connection_verified_at = self.clock()
        info["active_pair_available"] = bool(self.config and self.config.symbol in pairs)
        await self.broadcast_status()
        return info

    def live_ready(self) -> bool:
        return (
            self.connection_status == ConnectionStatus.CONNECTED
            and self.connection_verified_at is not None
            and self.clock() - self.connection_verified_at <= self.settings.live_connection_max_age_seconds
        )

    # ================================================================== controls

    async def configure(
        self,
        indicator: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        parameters: dict[str, Any] | None = None,
        position_size_type: str | None = None,
        position_size_value: Decimal | None = None,
    ) -> None:
        if self.running:
            raise TradingError("Stop automated trading before changing the strategy configuration")
        cfg = self.config
        new_indicator = indicator or cfg.indicator
        ind_cls = get_indicator(new_indicator)
        if parameters is None:
            parameters = cfg.parameters if new_indicator == cfg.indicator else ind_cls.default_settings()
        parameters = ind_cls.validate_settings(parameters)
        new_symbol = symbol or cfg.symbol
        if new_symbol != cfg.symbol:
            await self.market.get_pair(new_symbol)
            if await self._open_trade(symbol=cfg.symbol, any_mode=True):
                raise TradingError("Close the open position before switching trading pair")
        if position_size_type is not None and position_size_type not in ("fixed", "percent"):
            raise TradingError("position_size_type must be 'fixed' or 'percent'")
        if position_size_value is not None:
            if position_size_value <= 0:
                raise TradingError("position_size_value must be positive")
            if (position_size_type or cfg.position_size_type) == "percent" and position_size_value > 100:
                raise TradingError("Percentage position size cannot exceed 100")
        new_timeframe = timeframe or cfg.timeframe
        await self.market.configure(new_symbol, new_timeframe, new_indicator, parameters)
        await self._update_config(
            indicator=new_indicator,
            symbol=new_symbol,
            timeframe=new_timeframe,
            parameters=parameters,
            position_size_type=position_size_type or cfg.position_size_type,
            position_size_value=position_size_value if position_size_value is not None else cfg.position_size_value,
        )
        await self.paper.ensure_initialised(new_symbol.split("-")[1])
        await self.broadcast_status()

    async def configure_risk(self, **limits: Any) -> None:
        for key in ("max_position_size", "max_daily_loss", "min_balance"):
            if key in limits and limits[key] is not None and limits[key] < 0:
                raise TradingError(f"{key} cannot be negative")
        if "max_open_positions" in limits and (limits["max_open_positions"] is None or limits["max_open_positions"] < 1):
            raise TradingError("max_open_positions must be at least 1")
        await self._update_state(**limits)
        await self.broadcast_status()

    async def set_mode(self, mode: ExecutionMode, confirmation: str | None = None) -> None:
        if self.running:
            raise TradingError("Stop automated trading before changing execution mode")
        if mode == ExecutionMode.LIVE:
            if confirmation != LIVE_CONFIRMATION:
                raise TradingError(f"Live trading requires the confirmation phrase '{LIVE_CONFIRMATION}'")
            if self.live is None or self.live_client is None:
                raise TradingError("Revolut X API credentials are not configured")
            report = await self.live_startup_check()
            if not report["ok"]:
                raise TradingError("Live startup reconciliation failed: " + "; ".join(report["errors"]))
        self.mode = mode
        await self._update_config(execution_mode=mode.value)
        await self.broadcast_status()

    async def start(self) -> None:
        if self.emergency_stop:
            raise TradingError("Emergency stop is active. Reset it before starting")
        if self.blocked_reason:
            raise TradingError(self.blocked_reason)
        if self.mode == ExecutionMode.LIVE:
            report = await self.live_startup_check()
            if not report["ok"]:
                raise TradingError("Live startup reconciliation failed: " + "; ".join(report["errors"]))
        if self.market.is_stale():
            raise TradingError("Market data is stale; cannot start trading")
        self.running = True
        await self.broadcast_status()

    async def stop(self) -> None:
        self.running = False
        await self.broadcast_status()

    async def pause(self) -> None:
        await self._update_state(paused=True)
        await self.broadcast_status()

    async def resume(self) -> None:
        await self._update_state(paused=False)
        await self.broadcast_status()

    async def trigger_emergency_stop(self) -> None:
        """Halts all new automated orders immediately. Open positions are left untouched."""
        self.running = False
        await self._update_state(emergency_stop=True, paused=True)
        logger.warning("EMERGENCY STOP activated")
        await self.broadcast_status()

    async def reset_emergency_stop(self) -> None:
        await self._update_state(emergency_stop=False, paused=False)
        await self.broadcast_status()

    # ================================================================== signals

    async def on_signal(self, sig: StandardSignal) -> None:
        async with self._lock:
            cfg = self.config
            if cfg is None or (sig.symbol, sig.timeframe, sig.indicator) != (cfg.symbol, cfg.timeframe, cfg.indicator):
                return
            row = await self._persist_signal(sig)
            if row is None:
                logger.info("Duplicate signal ignored: %s %s @ %s", sig.signal_type, sig.symbol, sig.candle_ts)
                return
            reason = self._gate(sig)
            if reason is not None:
                await self._finish_signal(row.id, SignalStatus.SKIPPED, reason)
                return
            try:
                if sig.signal_type == SignalType.BUY:
                    await self._handle_buy(row)
                else:
                    await self._handle_sell(row)
            except Exception as exc:
                logger.exception("Signal execution failed")
                await self._finish_signal(row.id, SignalStatus.FAILED, str(exc))

    async def _persist_signal(self, sig: StandardSignal) -> Signal | None:
        try:
            async with self.db.transaction() as s:
                row = Signal(
                    config_id=self.config.id,
                    indicator=sig.indicator,
                    symbol=sig.symbol,
                    timeframe=sig.timeframe,
                    signal_type=sig.signal_type,
                    signal_price=Decimal(str(sig.price)),
                    candle_ts=from_ms(sig.candle_ts),
                    parameters=sig.parameters,
                    mode=self.mode.value,
                    execution_status=SignalStatus.PENDING,
                )
                s.add(row)
                await s.flush()
        except IntegrityError:
            return None
        await self.broadcaster.broadcast("indicator_signal", signal_dict(row))
        return row

    def _gate(self, sig: StandardSignal) -> str | None:
        if not self.running:
            return "Automated trading is stopped"
        if self.emergency_stop:
            return "Emergency stop active"
        if self.paused:
            return "Trading paused"
        if self.blocked_reason:
            return self.blocked_reason
        if self.market.is_stale():
            return "Market data is stale"
        step = interval_ms(sig.timeframe)
        if self.clock() * 1000 - (sig.candle_ts + step) > 2 * step + 60_000:
            return "Signal is too old to execute"
        if self.mode == ExecutionMode.LIVE and not self.live_ready():
            return "Revolut X connection not verified"
        return None

    async def _finish_signal(self, signal_id: int, status: SignalStatus, reason: str | None = None) -> None:
        async with self.db.transaction() as s:
            row = await s.get(Signal, signal_id)
            row.execution_status = status
            row.reason = reason
        if status == SignalStatus.SKIPPED:
            self.last_skip_reason = reason
        await self.broadcaster.broadcast("indicator_signal", signal_dict(row))

    async def _handle_buy(self, signal: Signal) -> None:
        cfg = self.config
        open_count = await self._open_trade_count()
        if await self._open_trade(symbol=cfg.symbol):
            await self._finish_signal(signal.id, SignalStatus.SKIPPED, "Position already open")
            return
        pair = await self.market.get_pair(cfg.symbol)
        if pair.status != "active":
            await self._finish_signal(signal.id, SignalStatus.SKIPPED, f"Pair {pair.symbol} is {pair.status}")
            return
        balances = await self.adapter.get_balance()
        available = balances.get(pair.quote, ZERO)
        requested = size_entry(cfg.position_size_type, cfg.position_size_value, available)
        decision = check_entry(self.limits, requested, available, open_count, await self.realized_pnl_today(), pair.min_order_size_quote)
        if not decision.allowed:
            await self._finish_signal(signal.id, SignalStatus.SKIPPED, decision.reason)
            return
        await self._submit(OrderSide.BUY, cfg.symbol, signal=signal, amount=decision.amount)

    async def _handle_sell(self, signal: Signal) -> None:
        trade = await self._open_trade(symbol=self.config.symbol)
        if trade is None:
            await self._finish_signal(signal.id, SignalStatus.SKIPPED, "No open position")
            return
        await self._sell_trade(trade, signal)

    async def _sell_trade(self, trade: Trade, signal: Signal | None) -> Order | None:
        if trade.config_id != self.config.id or trade.mode != self.mode.value:
            raise TradingError("Position does not belong to the active strategy/mode")
        pair = await self.market.get_pair(trade.symbol)
        qty = trade.open_quantity
        if self.mode == ExecutionMode.LIVE:
            available = (await self.adapter.get_balance()).get(pair.base, ZERO)
            qty = min(qty, available)
        if qty <= 0 or qty < pair.min_order_size:
            msg = f"Sell quantity {dec(qty)} below exchange minimum {dec(pair.min_order_size)}"
            if signal is not None:
                await self._finish_signal(signal.id, SignalStatus.SKIPPED, msg)
                return None
            raise TradingError(msg)
        return await self._submit(OrderSide.SELL, trade.symbol, signal=signal, quantity=qty, trade=trade)

    async def close_trade(self, trade_id: int) -> dict[str, Any]:
        """Manual market close of an open position (allowed while stopped/paused/emergency)."""
        async with self._lock:
            async with self.db.session() as s:
                trade = await s.get(Trade, trade_id)
            if trade is None or trade.status != TradeStatus.OPEN:
                raise TradingError("Trade is not open")
            if trade.mode != self.mode.value:
                raise TradingError(f"Switch to {trade.mode} mode to close this position")
            if self.blocked_reason:
                raise TradingError(self.blocked_reason)
            if self.mode == ExecutionMode.LIVE and not self.live_ready():
                await self.verify_connection()
            order = await self._sell_trade(trade, None)
            return order_dict(order) if order else {}

    # ================================================================== order submission

    async def _submit(
        self,
        side: OrderSide,
        symbol: str,
        signal: Signal | None = None,
        amount: Decimal | None = None,
        quantity: Decimal | None = None,
        trade: Trade | None = None,
    ) -> Order:
        coid = str(uuid.uuid4())
        async with self.db.transaction() as s:  # write-ahead: record intent before sending
            order = Order(
                client_order_id=coid,
                signal_id=signal.id if signal else None,
                mode=self.mode.value,
                symbol=symbol,
                side=side.value,
                order_type="market",
                requested_quote_amount=amount,
                requested_quantity=quantity,
                status=OrderStatus.CREATED,
            )
            s.add(order)
            await s.flush()
            if trade is not None:
                t = await s.get(Trade, trade.id)
                t.exit_order_id = order.id
        await self.broadcaster.broadcast("order_update", order_dict(order))
        adapter = self.adapter
        try:
            if side == OrderSide.BUY:
                result = await adapter.buy(symbol, amount, coid)
            else:
                result = await adapter.sell(symbol, quantity, coid)
        except OrderUncertainError as exc:
            self.blocked_reason = f"Order {coid} outcome unknown ({exc}); trading blocked until reconciled"
            await self._set_order(order.id, status=OrderStatus.UNKNOWN, reject_reason=str(exc))
            if signal:
                await self._finish_signal(signal.id, SignalStatus.FAILED, "Order outcome unknown; reconciling")
            await self.broadcast_status()
            return order
        except (ExecutionError, TradingError) as exc:
            await self._set_order(order.id, status=OrderStatus.FAILED, reject_reason=str(exc))
            if signal:
                await self._finish_signal(signal.id, SignalStatus.FAILED, str(exc))
            return order
        except Exception as exc:
            status = OrderStatus.UNKNOWN if self.mode == ExecutionMode.LIVE else OrderStatus.FAILED
            if status == OrderStatus.UNKNOWN:
                self.blocked_reason = f"Order {coid} failed unexpectedly; trading blocked until reconciled"
            await self._set_order(order.id, status=status, reject_reason=str(exc))
            if signal:
                await self._finish_signal(signal.id, SignalStatus.FAILED, str(exc))
            await self.broadcast_status()
            return order
        return await self._apply_result(order.id, result, signal.id if signal else None)

    async def _set_order(self, order_id: int, **fields: Any) -> Order:
        async with self.db.transaction() as s:
            order = await s.get(Order, order_id)
            for k, v in fields.items():
                setattr(order, k, v)
        await self.broadcaster.broadcast("order_update", order_dict(order))
        return order

    async def _apply_result(self, order_id: int, result: OrderResult, signal_id: int | None) -> Order:
        events: list[tuple[str, Any]] = []
        async with self.db.transaction() as s:
            order = await s.get(Order, order_id, with_for_update=True)
            order.status = result.status
            order.exchange_order_id = result.exchange_order_id or order.exchange_order_id
            order.filled_quantity = result.filled_quantity
            order.average_fill_price = result.average_fill_price
            order.fees = result.fee
            order.fee_currency = result.fee_currency
            order.reject_reason = result.reject_reason
            # Only apply once, and only when the order can no longer change.
            if result.is_terminal and result.filled_quantity > 0 and not order.applied:
                order.applied = True
                if order.side == OrderSide.BUY:
                    trade = await self._apply_buy(s, order, result, signal_id)
                    events.append(("trade_opened", trade))
                else:
                    trade = await self._apply_sell(s, order, result, signal_id)
                    if trade is not None:
                        events.append(("trade_closed" if trade.status == TradeStatus.CLOSED else "trade_updated", trade))
        await self.broadcaster.broadcast("order_update", order_dict(order))
        for event, trade in events:
            await self.broadcaster.broadcast(event, trade_dict(trade))
        if signal_id is not None:
            if result.filled_quantity > 0:
                status, reason = SignalStatus.EXECUTED, (None if result.status == OrderStatus.FILLED else f"Order {result.status}")
            elif result.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.FAILED):
                status, reason = SignalStatus.FAILED, result.reject_reason or f"Order {result.status}"
            else:
                status, reason = SignalStatus.PENDING, f"Order {result.status}; awaiting reconciliation"
            await self._finish_signal(signal_id, status, reason)
        await self.broadcast_balances()
        return order

    async def _signal_ts(self, s, signal_id: int | None) -> datetime | None:
        if signal_id is None:
            return None
        sig = await s.get(Signal, signal_id)
        return sig.candle_ts if sig else None

    async def _apply_buy(self, s, order: Order, r: OrderResult, signal_id: int | None) -> Trade:
        base = order.symbol.split("-")[0]
        quote_fee = r.fee_quote if r.fee_currency != base else ZERO
        trade = Trade(
            config_id=self.config.id,
            mode=order.mode,
            indicator=self.config.indicator,
            symbol=order.symbol,
            entry_order_id=order.id,
            entry_price=r.average_fill_price,
            quantity=r.net_base_quantity,
            open_quantity=r.net_base_quantity,
            entry_cost=r.filled_quantity * r.average_fill_price + quote_fee,
            entry_ts=utcnow(),
            entry_signal_ts=await self._signal_ts(s, signal_id),
            fees=r.fee_quote,
            status=TradeStatus.OPEN,
        )
        s.add(trade)
        await s.flush()
        return trade

    async def _apply_sell(self, s, order: Order, r: OrderResult, signal_id: int | None) -> Trade | None:
        trade = (await s.execute(select(Trade).where(Trade.exit_order_id == order.id).with_for_update())).scalars().first()
        if trade is None:
            logger.error("Sell order %s filled but no matching trade", order.client_order_id)
            return None
        sold = min(r.filled_quantity, trade.open_quantity)
        proceeds = r.filled_quantity * r.average_fill_price - r.fee_quote
        cost_portion = trade.entry_cost * sold / trade.quantity
        previously_sold = trade.quantity - trade.open_quantity
        trade.exit_price = (
            r.average_fill_price
            if previously_sold <= 0 or trade.exit_price is None
            else (trade.exit_price * previously_sold + r.average_fill_price * sold) / (previously_sold + sold)
        )
        trade.exit_proceeds = (trade.exit_proceeds or ZERO) + proceeds
        trade.realized_pnl = (trade.realized_pnl or ZERO) + proceeds - cost_portion
        trade.fees = (trade.fees or ZERO) + r.fee_quote
        trade.open_quantity = trade.open_quantity - sold
        trade.exit_signal_ts = await self._signal_ts(s, signal_id) or trade.exit_signal_ts
        pair = self.market.pairs.get(trade.symbol)
        dust = pair.min_order_size if pair else ZERO
        if trade.open_quantity <= 0 or trade.open_quantity < dust:
            trade.status = TradeStatus.CLOSED
            trade.exit_ts = utcnow()
        await s.flush()
        return trade

    # ================================================================== reconciliation

    async def reconcile(self) -> None:
        """Resolve orders whose final state is unknown locally (timeouts, restarts)."""
        async with self.db.session() as s:
            pending = (
                await s.execute(
                    select(Order).where(
                        (Order.status.in_([x.value for x in OPEN_ORDER_STATUSES]))
                        | ((Order.status == OrderStatus.FILLED.value) & (Order.applied.is_(False)) & (Order.filled_quantity > 0))
                    )
                )
            ).scalars().all()
        unresolved = []
        for order in pending:
            try:
                resolved = await self._reconcile_order(order)
            except Exception as exc:
                logger.warning("Reconciliation of %s failed: %s", order.client_order_id, exc)
                resolved = False
            if not resolved:
                unresolved.append(order)
        live_unknown = [o for o in unresolved if o.mode == ExecutionMode.LIVE and o.status in (OrderStatus.UNKNOWN, OrderStatus.CREATED)]
        if live_unknown:
            self.blocked_reason = f"{len(live_unknown)} live order(s) with unknown outcome; trading blocked until reconciled"
        elif self.blocked_reason:
            self.blocked_reason = None
            await self.broadcast_status()

    async def _reconcile_order(self, order: Order) -> bool:
        age = (utcnow() - order.created_at.replace(tzinfo=order.created_at.tzinfo or timezone.utc)).total_seconds()
        if order.mode == ExecutionMode.PAPER:
            found = await self.paper.find_order(order.client_order_id, order.symbol)
            if found is None:
                await self._set_order(order.id, status=OrderStatus.FAILED, reject_reason="Paper order lost (restart)")
                return True
            await self._apply_result(order.id, found, order.signal_id)
            return True
        if self.live is None or not self.live_ready():
            return False
        if order.exchange_order_id:
            result = await self.live.get_order_status(order.exchange_order_id)
        else:
            result = await self.live.find_order(order.client_order_id, order.symbol)
        if result is None:
            if age > UNRESOLVED_GRACE_SECONDS:
                await self._set_order(order.id, status=OrderStatus.FAILED, reject_reason="Not found on exchange; treated as not placed")
                return True
            return False
        result.client_order_id = order.client_order_id
        await self._apply_result(order.id, result, order.signal_id)
        return result.is_terminal

    async def live_startup_check(self) -> dict[str, Any]:
        """Full exchange reconciliation required before any live order may be sent.

        1. Authenticated connection and pair availability.
        2. Resolve every local order whose outcome is not final (by exchange id or client order id).
        3. Active orders on Revolut X for the pair: any open order blocks live trading.
        4. Recent historical orders: fills of our own orders must match what we recorded.
        5. Balances: open LIVE positions must be covered by the exchange balance.
        """
        errors: list[str] = []
        warnings: list[str] = []
        symbol = self.config.symbol
        try:
            info = await self.verify_connection()
            if not info.get("active_pair_available"):
                errors.append(f"Pair {symbol} is not available for your Revolut X account")
        except TradingError as exc:
            errors.append(str(exc))
        if not errors and self.live is not None:
            try:
                await self.reconcile()
                if self.blocked_reason:
                    errors.append(self.blocked_reason)
                await self._check_exchange_orders(symbol, errors, warnings)
                await self.reconcile_live_positions()
                errors.extend(self.reconciliation_warnings)
                warnings.extend(await self._untracked_balance_warnings(symbol))
            except (RevolutXError, TradingError) as exc:
                errors.append(f"Reconciliation request failed: {exc}")
        self.live_check = {"ok": not errors, "checked_at": int(self.clock() * 1000), "symbol": symbol, "errors": errors, "warnings": warnings}
        await self.broadcast_status()
        return self.live_check

    async def _check_exchange_orders(self, symbol: str, errors: list[str], warnings: list[str]) -> None:
        assert self.live is not None
        async with self.db.session() as s:
            local = {o.client_order_id: o for o in (await s.execute(select(Order).where(Order.mode == "LIVE", Order.symbol == symbol))).scalars().all()}
        active = await self.live.get_active_orders(symbol)
        for raw in active:
            coid = raw.get("client_order_id")
            owner = "this app" if coid in local else "outside this app"
            errors.append(f"Open order {raw.get('id')} ({raw.get('side')} {raw.get('quantity') or raw.get('quote_quantity') or ''}) on Revolut X placed {owner}; cancel it or wait for it to finish")
        since = int((self.clock() - 7 * 86400) * 1000)
        for raw in await self.live.get_recent_orders(symbol, since):
            coid = raw.get("client_order_id")
            mine = local.get(coid) if coid else None
            if mine is None:
                if to_decimal(raw.get("filled_quantity"), ZERO) > 0:
                    warnings.append(f"Exchange order {raw.get('id')} ({raw.get('side')}) was filled outside this app in the last 7 days; it is not tracked as a strategy position")
                continue
            ex_filled = to_decimal(raw.get("filled_quantity"), ZERO)
            if mine.filled_quantity != ex_filled:
                errors.append(f"Order {coid}: exchange filled {dec(ex_filled)} but local record shows {dec(mine.filled_quantity)}")

    async def _untracked_balance_warnings(self, symbol: str) -> list[str]:
        assert self.live is not None
        base = symbol.split("-")[0]
        balances = await self.live.get_balance()
        open_trade = await self._open_trade(symbol)
        tracked = open_trade.open_quantity if open_trade and open_trade.mode == ExecutionMode.LIVE else ZERO
        held = balances.get(base, ZERO)
        if held > tracked:
            return [f"{dec(held - tracked)} {base} on Revolut X is not part of a strategy position and will never be sold by the bot"]
        return []

    async def reconcile_live_positions(self) -> None:
        self.reconciliation_warnings = []
        if self.live is None:
            return
        balances = await self.live.get_balance()
        async with self.db.session() as s:
            trades = (await s.execute(select(Trade).where(Trade.mode == "LIVE", Trade.status == TradeStatus.OPEN))).scalars().all()
        for t in trades:
            base = t.symbol.split("-")[0]
            if balances.get(base, ZERO) < t.open_quantity:
                self.reconciliation_warnings.append(
                    f"Open LIVE trade #{t.id} expects {dec(t.open_quantity)} {base} but exchange shows {dec(balances.get(base, ZERO))} available"
                )

    async def reconcile_loop(self, stop: asyncio.Event, interval: float = 10.0) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                async with self._lock:
                    await self.reconcile()
            except Exception:
                logger.exception("Reconcile loop error")

    # ================================================================== queries

    async def _open_trade(self, symbol: str, any_mode: bool = False) -> Trade | None:
        async with self.db.session() as s:
            q = select(Trade).where(Trade.status == TradeStatus.OPEN, Trade.symbol == symbol)
            if not any_mode:
                q = q.where(Trade.mode == self.mode.value)
            return (await s.execute(q.order_by(Trade.id))).scalars().first()

    async def _open_trade_count(self) -> int:
        async with self.db.session() as s:
            return (await s.execute(select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.OPEN, Trade.mode == self.mode.value))).scalar_one()

    async def realized_pnl_today(self) -> Decimal:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.db.session() as s:
            total = (
                await s.execute(
                    select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(
                        Trade.mode == self.mode.value, Trade.updated_at >= midnight, Trade.realized_pnl != 0
                    )
                )
            ).scalar_one()
        return Decimal(str(total))

    async def _bid(self) -> Decimal | None:
        t = self.market.ticker
        return Decimal(str(t["bid"])) if t and t.get("bid") else None

    async def trades(self, status: str | None = None, mode: str | None = None, limit: int = 200) -> list[dict]:
        mode = mode or self.mode.value
        async with self.db.session() as s:
            q = select(Trade).where(Trade.mode == mode)
            if status:
                q = q.where(Trade.status == status)
            rows = (await s.execute(q.order_by(Trade.id.desc()).limit(limit))).scalars().all()
        bid = await self._bid()
        return [trade_dict(t, bid if t.symbol == self.market.symbol else None) for t in rows]

    async def orders(self, limit: int = 100) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Order).order_by(Order.id.desc()).limit(limit))).scalars().all()
        return [order_dict(o) for o in rows]

    async def signals(self, limit: int = 200) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Signal).order_by(Signal.id.desc()).limit(limit))).scalars().all()
        return [signal_dict(r) for r in rows]

    async def performance(self, mode: str | None = None) -> dict[str, Any]:
        mode = mode or self.mode.value
        async with self.db.session() as s:
            closed = (await s.execute(select(Trade).where(Trade.mode == mode, Trade.status == TradeStatus.CLOSED).order_by(Trade.exit_ts))).scalars().all()
            open_ = (await s.execute(select(Trade).where(Trade.mode == mode, Trade.status == TradeStatus.OPEN))).scalars().all()
        pnls = [t.realized_pnl for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        equity, peak, max_dd = ZERO, ZERO, ZERO
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        bid = await self._bid()
        unrealized = sum(
            (t.open_quantity * bid - t.entry_cost * t.open_quantity / t.quantity for t in open_ if bid is not None and t.symbol == self.market.symbol and t.quantity > 0),
            ZERO,
        )
        partial_realized = sum((t.realized_pnl for t in open_), ZERO)
        gross_loss = -sum(losses, ZERO)
        return {
            "mode": mode,
            "total_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": dec(Decimal(len(wins)) / len(closed) * 100) if closed else None,
            "total_realized_pnl": dec(sum(pnls, ZERO) + partial_realized),
            "unrealized_pnl": dec(unrealized),
            "average_win": dec(sum(wins, ZERO) / len(wins)) if wins else None,
            "average_loss": dec(sum(losses, ZERO) / len(losses)) if losses else None,
            "profit_factor": dec(sum(wins, ZERO) / gross_loss) if gross_loss > 0 else None,
            "max_drawdown": dec(max_dd),
            "total_fees": dec(sum((t.fees for t in closed + list(open_)), ZERO)),
            "open_positions": len(open_),
            "realized_pnl_today": dec(await self.realized_pnl_today()) if mode == self.mode.value else None,
        }

    async def balances(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode.value}
        try:
            out["balances"] = {k: dec(v) for k, v in (await self.adapter.get_balance()).items()}
        except Exception as exc:
            out["balances"] = {}
            out["error"] = str(exc)
        return out

    def status(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "mode": self.mode.value,
            "running": self.running,
            "paused": self.paused,
            "emergency_stop": self.emergency_stop,
            "blocked_reason": self.blocked_reason,
            "last_skip_reason": self.last_skip_reason,
            "connection_status": self.connection_status.value,
            "connection_error": self.connection_error,
            "credentials_configured": self.live_client is not None,
            "live_ready": self.live_ready(),
            "live_confirmation_phrase": LIVE_CONFIRMATION,
            "market_data_stale": self.market.is_stale(),
            "market_data_status": self.market.data_status(),
            "poll_interval_seconds": self.settings.market_poll_interval,
            "stale_data_seconds": self.settings.stale_data_seconds,
            "market_data_error": self.market.last_error,
            "last_market_update": int(self.market.last_ticker_ok * 1000) if self.market.last_ticker_ok else None,
            "reconciliation_warnings": self.reconciliation_warnings,
            "live_check": self.live_check,
            "config": {
                "indicator": cfg.indicator,
                "symbol": cfg.symbol,
                "timeframe": cfg.timeframe,
                "parameters": cfg.parameters,
                "position_size_type": cfg.position_size_type,
                "position_size_value": dec(cfg.position_size_value),
            }
            if cfg
            else None,
            "risk": {
                "max_position_size": dec(self.limits.max_position_size),
                "max_daily_loss": dec(self.limits.max_daily_loss),
                "max_open_positions": self.limits.max_open_positions,
                "min_balance": dec(self.limits.min_balance),
            },
        }

    async def _on_data_status(self, status: str) -> None:
        logger.info("Market data status: %s", status)
        await self.broadcast_status()

    async def broadcast_status(self) -> None:
        await self.broadcaster.broadcast("trading_status", self.status())

    async def broadcast_balances(self) -> None:
        await self.broadcaster.broadcast("balance_update", await self.balances())
