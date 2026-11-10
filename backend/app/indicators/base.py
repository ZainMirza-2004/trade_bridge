"""Indicator interface.

Every indicator is a deterministic state machine over *closed* candles: ``IndicatorStream.update``
consumes one candle and returns the indicator values for that candle plus an optional signal.
``calculate`` runs the very same stream over a history, so batch and incremental results are
identical by construction and no value can depend on a future candle (no lookahead).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

NAN = float("nan")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    type: str  # "int" | "float" | "bool"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    description: str = ""


@dataclass(frozen=True)
class Candle:
    time: int  # candle open time, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class IndicatorPoint:
    time: int
    values: dict[str, float | int | None]
    signal: str | None = None  # "BUY" | "SELL" | None


@dataclass
class StandardSignal:
    indicator: str
    symbol: str
    timeframe: str
    signal_type: str
    candle_ts: int
    price: float
    confirmed: bool
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WilderATR:
    """Pine ``ta.atr(length)``: RMA of true range, seeded with the SMA of the first ``length`` TRs.
    The first bar's TR is high-low (previous close unavailable)."""

    def __init__(self, length: int):
        self.length = length
        self.prev_close: float | None = None
        self.count = 0
        self._sum = 0.0
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        self.count += 1
        if self.count < self.length:
            self._sum += tr
        elif self.count == self.length:
            self._sum += tr
            self.value = self._sum / self.length
        else:
            self.value = (self.value * (self.length - 1) + tr) / self.length  # type: ignore[operator]
        return self.value


class IndicatorStream(ABC):
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.last_time: int | None = None

    def update(self, candle: Candle) -> IndicatorPoint:
        if self.last_time is not None and candle.time <= self.last_time:
            raise ValueError("Candles must be fed in strictly increasing time order (closed candles only)")
        self.last_time = candle.time
        return self._step(candle)

    @abstractmethod
    def _step(self, candle: Candle) -> IndicatorPoint: ...


class BaseIndicator(ABC):
    name: str
    label: str
    description: str
    params: list[ParamSpec]
    overlay_keys: list[str]

    # ---- parameters

    @classmethod
    def default_settings(cls) -> dict[str, Any]:
        return {p.name: p.default for p in cls.params}

    @classmethod
    def validate_settings(cls, settings: dict[str, Any] | None) -> dict[str, Any]:
        out = cls.default_settings()
        specs = {p.name: p for p in cls.params}
        for key, value in (settings or {}).items():
            if key not in specs:
                raise ValueError(f"Unknown parameter '{key}' for {cls.label}")
            spec = specs[key]
            if spec.type == "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"{spec.label} must be true or false")
                out[key] = value
                continue
            try:
                num = int(value) if spec.type == "int" else float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{spec.label} must be a number") from None
            if spec.type == "int" and float(value) != num:
                raise ValueError(f"{spec.label} must be an integer")
            if not math.isfinite(num):
                raise ValueError(f"{spec.label} must be finite")
            if spec.min is not None and num < spec.min:
                raise ValueError(f"{spec.label} must be >= {spec.min}")
            if spec.max is not None and num > spec.max:
                raise ValueError(f"{spec.label} must be <= {spec.max}")
            out[key] = num
        return out

    @classmethod
    def get_metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "label": cls.label,
            "description": cls.description,
            "params": [asdict(p) for p in cls.params],
            "overlay_keys": cls.overlay_keys,
        }

    @classmethod
    def warmup_candles(cls, settings: dict[str, Any]) -> int:
        return int(settings.get("atr_period", 10)) + 1

    # ---- calculation

    @classmethod
    @abstractmethod
    def create_stream(cls, settings: dict[str, Any]) -> IndicatorStream: ...

    @classmethod
    def calculate(cls, candles: pd.DataFrame | list[Candle], settings: dict[str, Any] | None = None) -> pd.DataFrame:
        """Compute indicator values for every (closed) candle. Returns a DataFrame indexed like the input
        with one column per value plus ``signal``."""
        settings = cls.validate_settings(settings)
        rows = candles_to_list(candles)
        stream = cls.create_stream(settings)
        points = [stream.update(c) for c in rows]
        return points_to_frame(points)

    @classmethod
    def generate_signals(
        cls,
        indicator_data: pd.DataFrame,
        symbol: str = "",
        timeframe: str = "",
        settings: dict[str, Any] | None = None,
        confirmed: bool = True,
    ) -> list[StandardSignal]:
        settings = cls.validate_settings(settings)
        out: list[StandardSignal] = []
        for row in indicator_data[indicator_data["signal"].notna()].itertuples():
            out.append(
                StandardSignal(
                    indicator=cls.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_type=row.signal,
                    candle_ts=int(row.time),
                    price=float(row.close),
                    confirmed=confirmed,
                    parameters=settings,
                )
            )
        return out


def candles_to_list(candles: pd.DataFrame | list[Candle]) -> list[Candle]:
    if isinstance(candles, list):
        return candles
    cols = {c.lower(): c for c in candles.columns}
    t = candles[cols["time"]].to_numpy(dtype=np.int64)
    o = candles[cols["open"]].to_numpy(dtype=float)
    h = candles[cols["high"]].to_numpy(dtype=float)
    lo = candles[cols["low"]].to_numpy(dtype=float)
    c = candles[cols["close"]].to_numpy(dtype=float)
    v = candles[cols["volume"]].to_numpy(dtype=float) if "volume" in cols else np.zeros(len(candles))
    return [Candle(int(t[i]), o[i], h[i], lo[i], c[i], v[i]) for i in range(len(candles))]


def points_to_frame(points: list[IndicatorPoint]) -> pd.DataFrame:
    if not points:
        return pd.DataFrame(columns=["time", "signal"])
    records = [{"time": p.time, **p.values, "signal": p.signal} for p in points]
    return pd.DataFrame.from_records(records)


def nan_to_none(value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value
