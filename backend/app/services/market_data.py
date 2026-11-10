"""Market data via REST polling (Revolut X has no public WebSocket feed).

Holds the candle buffer and indicator state for the single active strategy, detects newly
closed candles and hands confirmed signals to the trading engine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.core.decimal_utils import to_decimal
from app.core.timeframes import interval_minutes, interval_ms
from app.exchanges.revolut_x import PUBLIC_CANDLE_LIMIT, PairInfo, RevolutXClient, RevolutXError
from app.indicators.base import BaseIndicator, Candle, IndicatorPoint, StandardSignal, nan_to_none
from app.indicators.registry import get_indicator
from app.websocket.manager import Broadcaster

logger = logging.getLogger(__name__)

CLOSE_GRACE_MS = 2_000
HISTORY_CANDLES = PUBLIC_CANDLE_LIMIT - 1
PAIR_REFRESH_SECONDS = 3600

SignalHandler = Callable[[StandardSignal], Awaitable[None]]


def parse_candle(raw: dict) -> Candle:
    return Candle(
        time=int(raw["start"]),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw.get("volume") or 0),
    )


def candle_dict(c: Candle) -> dict[str, Any]:
    return {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}


def point_dict(p: IndicatorPoint) -> dict[str, Any]:
    return {"time": p.time, "values": {k: nan_to_none(v) for k, v in p.values.items()}, "signal": p.signal}


class MarketDataService:
    def __init__(
        self,
        client: RevolutXClient,
        settings: Settings,
        broadcaster: Broadcaster,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.settings = settings
        self.broadcaster = broadcaster
        self.clock = clock
        self.on_signal: SignalHandler | None = None
        self.on_data_status: Callable[[str], Awaitable[None]] | None = None
        self._last_data_status: str | None = None

        self.symbol: str | None = None
        self.timeframe: str | None = None
        self.indicator: type[BaseIndicator] | None = None
        self.indicator_settings: dict[str, Any] = {}

        self.closed: list[Candle] = []
        self.forming: Candle | None = None
        self.points: list[IndicatorPoint] = []
        self.signals: list[StandardSignal] = []
        self._stream = None

        self.pairs: dict[str, PairInfo] = {}
        self._pairs_loaded_at = 0.0
        self.ticker: dict[str, Any] | None = None
        self.last_ticker_ok: float | None = None
        self.last_candles_ok: float | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ pairs / prices

    async def load_pairs(self, force: bool = False) -> dict[str, PairInfo]:
        if force or not self.pairs or self.clock() - self._pairs_loaded_at > PAIR_REFRESH_SECONDS:
            self.pairs = await self.client.get_public_pairs()
            self._pairs_loaded_at = self.clock()
        return self.pairs

    async def get_pair(self, symbol: str) -> PairInfo:
        pairs = await self.load_pairs()
        if symbol not in pairs:
            raise ValueError(f"Unknown trading pair {symbol}")
        return pairs[symbol]

    async def get_quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        """(bid, ask). Uses the cached ticker when fresh, otherwise fetches one."""
        if self.ticker and self.ticker.get("symbol") == symbol and not self.ticker_stale():
            t = self.ticker
        else:
            rows = await self.client.get_public_tickers([symbol.replace("-", "/")])
            if not rows:
                raise RevolutXError(f"No ticker for {symbol}")
            t = self._normalise_ticker(rows[0])
        return to_decimal(t["bid"]), to_decimal(t["ask"])

    @staticmethod
    def _normalise_ticker(raw: dict) -> dict[str, Any]:
        return {
            "symbol": (raw.get("symbol") or "").replace("/", "-"),
            "bid": raw.get("bid"),
            "ask": raw.get("ask"),
            "mid": raw.get("mid"),
            "last_price": raw.get("last_price"),
            "high_24h": raw.get("high_24h"),
            "low_24h": raw.get("low_24h"),
            "price_change_24h": raw.get("price_change_24h"),
            "volume_24h": raw.get("volume_24h"),
        }

    # ------------------------------------------------------------------ staleness

    def ticker_stale(self) -> bool:
        return self.last_ticker_ok is None or self.clock() - self.last_ticker_ok > self.settings.stale_data_seconds

    def candles_stale(self) -> bool:
        if self.last_candles_ok is None:
            return True
        return self.clock() - self.last_candles_ok > self.settings.stale_data_seconds

    def is_stale(self) -> bool:
        return self.ticker_stale() or self.candles_stale()

    def data_status(self) -> str:
        """LIVE: recent successful polls. STALE: last success too old. DISCONNECTED: never reached / failing."""
        last_ok = max(self.last_ticker_ok or 0, self.last_candles_ok or 0)
        if not last_ok:
            return "DISCONNECTED"
        if not self.is_stale():
            return "LIVE"
        if self.last_error and self.clock() - last_ok > 3 * self.settings.stale_data_seconds:
            return "DISCONNECTED"
        return "STALE"

    # ------------------------------------------------------------------ stream configuration

    async def configure(self, symbol: str, timeframe: str, indicator: str, parameters: dict[str, Any]) -> None:
        indicator_cls = get_indicator(indicator)
        params = indicator_cls.validate_settings(parameters)
        interval_minutes(timeframe)
        async with self._lock:
            changed_market = (symbol, timeframe) != (self.symbol, self.timeframe)
            changed_symbol = symbol != self.symbol
            self.symbol, self.timeframe = symbol, timeframe
            self.indicator, self.indicator_settings = indicator_cls, params
            if changed_symbol:
                self.ticker = None
                self.last_ticker_ok = None
            if changed_market:
                await self._load_history()
            self._recompute()

    async def _load_history(self) -> None:
        assert self.symbol and self.timeframe
        step = interval_ms(self.timeframe)
        now = int(self.clock() * 1000)
        since = now - HISTORY_CANDLES * step
        raw = await self.client.get_public_candles(self.symbol, interval_minutes(self.timeframe), since=since, until=now)
        self.closed, self.forming = [], None
        self._merge(raw, now)
        self.last_candles_ok = self.clock()

    def _merge(self, raw: list[dict], fetched_at_ms: int) -> list[Candle]:
        """Merge fetched candles; returns candles that became closed by this merge (oldest first)."""
        step = interval_ms(self.timeframe)  # type: ignore[arg-type]
        last_closed = self.closed[-1].time if self.closed else None
        newly_closed: list[Candle] = []
        forming: Candle | None = None
        for c in sorted((parse_candle(r) for r in raw), key=lambda c: c.time):
            if last_closed is not None and c.time <= last_closed:
                continue
            if c.time + step + CLOSE_GRACE_MS <= fetched_at_ms:
                newly_closed.append(c)
                last_closed = c.time
            else:
                forming = c
        self.closed.extend(newly_closed)
        if len(self.closed) > 5000:
            del self.closed[: len(self.closed) - 5000]
        self.forming = forming
        return newly_closed

    def _recompute(self) -> None:
        assert self.indicator is not None
        self._stream = self.indicator.create_stream(self.indicator_settings)
        self.points = [self._stream.update(c) for c in self.closed]
        self.signals = [self._signal_from(p, c) for p, c in zip(self.points, self.closed) if p.signal]

    def _signal_from(self, p: IndicatorPoint, c: Candle) -> StandardSignal:
        assert self.indicator and self.symbol and self.timeframe
        return StandardSignal(
            indicator=self.indicator.name,
            symbol=self.symbol,
            timeframe=self.timeframe,
            signal_type=p.signal,  # type: ignore[arg-type]
            candle_ts=c.time,
            price=c.close,
            confirmed=True,
            parameters=dict(self.indicator_settings),
        )

    # ------------------------------------------------------------------ polling

    async def poll_candles(self) -> list[StandardSignal]:
        if not (self.symbol and self.timeframe and self.indicator):
            return []
        async with self._lock:
            step = interval_ms(self.timeframe)
            now = int(self.clock() * 1000)
            earliest = now - HISTORY_CANDLES * step
            since = max(self.closed[-1].time - step, earliest) if self.closed else earliest
            raw = await self.client.get_public_candles(self.symbol, interval_minutes(self.timeframe), since=since, until=now)
            self.last_candles_ok = self.clock()
            newly_closed = self._merge(raw, now)
            new_signals: list[StandardSignal] = []
            for c in newly_closed:
                p = self._stream.update(c)
                self.points.append(p)
                if p.signal:
                    sig = self._signal_from(p, c)
                    self.signals.append(sig)
                    new_signals.append(sig)
                await self.broadcaster.broadcast(
                    "candle_update", {"symbol": self.symbol, "timeframe": self.timeframe, "candle": candle_dict(c), "closed": True, "point": point_dict(p)}
                )
            if self.forming is not None:
                await self.broadcaster.broadcast(
                    "candle_update", {"symbol": self.symbol, "timeframe": self.timeframe, "candle": candle_dict(self.forming), "closed": False, "point": None}
                )
        for sig in new_signals:
            await self.broadcaster.broadcast("indicator_signal", sig.to_dict())
            if self.on_signal is not None:
                try:
                    await self.on_signal(sig)
                except Exception:  # never let a trading failure kill the market loop
                    logger.exception("Signal handler failed")
        return new_signals

    async def poll_ticker(self) -> None:
        if not self.symbol:
            return
        rows = await self.client.get_public_tickers([self.symbol.replace("-", "/")])
        if rows:
            self.ticker = self._normalise_ticker(rows[0])
            self.last_ticker_ok = self.clock()
            await self.broadcaster.broadcast("market_update", self.ticker)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for poll in (self.poll_ticker, self.poll_candles):
                try:
                    await poll()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.warning("Market data poll failed: %s", exc)
            await self._emit_data_status()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.market_poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _emit_data_status(self) -> None:
        current = self.data_status()
        if current != self._last_data_status:
            self._last_data_status = current
            if self.on_data_status is not None:
                try:
                    await self.on_data_status(current)
                except Exception:
                    logger.exception("Data status handler failed")

    # ------------------------------------------------------------------ views

    def chart_payload(self) -> dict[str, Any]:
        candles = [candle_dict(c) for c in self.closed]
        if self.forming is not None:
            candles.append(candle_dict(self.forming))
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "indicator": self.indicator.get_metadata() if self.indicator else None,
            "parameters": self.indicator_settings,
            "candles": candles,
            "points": [point_dict(p) for p in self.points],
            "signals": [s.to_dict() for s in self.signals],
            "forming_closed": False,
        }
