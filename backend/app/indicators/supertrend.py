"""Supertrend — port of Pine's built-in ``ta.supertrend(factor, atrPeriod)`` which the reference
repository (vendor/supertrend-reference/SuperTrend.pine, MIT) calls:

    src = hl2
    atr = ta.atr(atrPeriod)
    upperBand = src + factor * atr
    lowerBand = src - factor * atr
    prevLowerBand = nz(lowerBand[1]); prevUpperBand = nz(upperBand[1])
    lowerBand := lowerBand > prevLowerBand or close[1] < prevLowerBand ? lowerBand : prevLowerBand
    upperBand := upperBand < prevUpperBand or close[1] > prevUpperBand ? upperBand : prevUpperBand
    if na(atr[1])                         direction := 1
    else if superTrend[1] == upperBand[1] direction := close > upperBand ? -1 : 1
    else                                  direction := close < lowerBand ? 1 : -1
    superTrend := direction == -1 ? lowerBand : upperBand

    buy  = direction < 0 and direction[1] > 0
    sell = direction > 0 and direction[1] < 0

``direction == -1`` is an uptrend (line below price).
"""

from __future__ import annotations

import math

from .base import NAN, BaseIndicator, Candle, IndicatorPoint, IndicatorStream, ParamSpec, WilderATR


def _nz(x: float) -> float:
    return 0.0 if math.isnan(x) else x


class SupertrendStream(IndicatorStream):
    def __init__(self, params):
        super().__init__(params)
        self.factor = float(params["multiplier"])
        self.atr = WilderATR(int(params["atr_period"]))
        self.prev_atr: float | None = None
        self.prev_close: float | None = None
        self.prev_lower = NAN
        self.prev_upper = NAN
        self.prev_st = NAN
        self.prev_dir: int | None = None

    def _step(self, c: Candle) -> IndicatorPoint:
        hl2 = (c.high + c.low) / 2.0
        atr = self.atr.update(c.high, c.low, c.close)
        upper = hl2 + self.factor * atr if atr is not None else NAN
        lower = hl2 - self.factor * atr if atr is not None else NAN

        pl, pu = _nz(self.prev_lower), _nz(self.prev_upper)
        pc = self.prev_close
        lower_f = lower if ((not math.isnan(lower) and lower > pl) or (pc is not None and pc < pl)) else pl
        upper_f = upper if ((not math.isnan(upper) and upper < pu) or (pc is not None and pc > pu)) else pu

        if self.prev_atr is None:
            direction = 1
        elif self.prev_st == self.prev_upper:
            direction = -1 if c.close > upper_f else 1
        else:
            direction = 1 if c.close < lower_f else -1
        st = lower_f if direction == -1 else upper_f

        signal = None
        if self.prev_dir is not None:
            if direction < 0 and self.prev_dir > 0:
                signal = "BUY"
            elif direction > 0 and self.prev_dir < 0:
                signal = "SELL"

        self.prev_atr = atr
        self.prev_close = c.close
        self.prev_lower, self.prev_upper, self.prev_st, self.prev_dir = lower_f, upper_f, st, direction

        valid = atr is not None
        return IndicatorPoint(
            time=c.time,
            values={
                "close": c.close,
                "atr": atr if valid else NAN,
                "upper_band": upper_f if valid else NAN,
                "lower_band": lower_f if valid else NAN,
                "supertrend": st if valid else NAN,
                "direction": direction,
            },
            signal=signal,
        )


class SupertrendIndicator(BaseIndicator):
    name = "supertrend"
    label = "Supertrend"
    description = "ATR band trend follower. Buy when the trend flips bullish, sell when it flips bearish."
    params = [
        ParamSpec("atr_period", "ATR Period", "int", 10, min=1, max=500, step=1),
        ParamSpec("multiplier", "Multiplier", "float", 3.0, min=0.1, max=50, step=0.1),
    ]
    overlay_keys = ["supertrend"]

    @classmethod
    def create_stream(cls, settings) -> SupertrendStream:
        return SupertrendStream(cls.validate_settings(settings))
