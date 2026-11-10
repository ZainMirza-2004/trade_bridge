"""UT Bot Alerts (QuantNomad, https://www.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/).

Faithful port of the original Pine v4 logic:

    xATR  = atr(c)
    nLoss = a * xATR
    src   = h ? heikinashi close : close
    xATRTrailingStop := iff(src > nz(stop[1]) and src[1] > nz(stop[1]), max(nz(stop[1]), src - nLoss),
                        iff(src < nz(stop[1]) and src[1] < nz(stop[1]), min(nz(stop[1]), src + nLoss),
                        iff(src > nz(stop[1]), src - nLoss, src + nLoss)))
    pos   := iff(src[1] < nz(stop[1]) and src > nz(stop[1]), 1,
             iff(src[1] > nz(stop[1]) and src < nz(stop[1]), -1, nz(pos[1])))
    ema   = ema(src, 1)            // == src
    buy   = src > stop and crossover(ema, stop)
    sell  = src < stop and crossover(stop, ema)

Heikin Ashi close = (open + high + low + close) / 4; the ATR is always computed on regular candles,
exactly as in the original script (``atr(c)`` uses the chart's OHLC).
"""

from __future__ import annotations

import math

from .base import NAN, BaseIndicator, Candle, IndicatorPoint, IndicatorStream, ParamSpec, WilderATR


class UTBotStream(IndicatorStream):
    def __init__(self, params):
        super().__init__(params)
        self.key = float(params["key_value"])
        self.atr = WilderATR(int(params["atr_period"]))
        self.heikin_ashi = bool(params["heikin_ashi"])
        self.prev_src: float | None = None
        self.prev_stop: float = NAN
        self.pos = 0

    def _step(self, c: Candle) -> IndicatorPoint:
        src = (c.open + c.high + c.low + c.close) / 4.0 if self.heikin_ashi else c.close
        atr = self.atr.update(c.high, c.low, c.close)
        n_loss = self.key * atr if atr is not None else NAN

        prev_stop_raw = self.prev_stop
        p = 0.0 if math.isnan(prev_stop_raw) else prev_stop_raw  # nz(xATRTrailingStop[1], 0)
        s1 = self.prev_src

        if math.isnan(n_loss):
            stop = NAN
        elif s1 is not None and src > p and s1 > p:
            stop = max(p, src - n_loss)
        elif s1 is not None and src < p and s1 < p:
            stop = min(p, src + n_loss)
        elif src > p:
            stop = src - n_loss
        else:
            stop = src + n_loss

        if s1 is not None and s1 < p and src > p:
            self.pos = 1
        elif s1 is not None and s1 > p and src < p:
            self.pos = -1

        signal = None
        if s1 is not None and not math.isnan(stop) and not math.isnan(prev_stop_raw):
            if src > stop and s1 <= prev_stop_raw:
                signal = "BUY"
            elif src < stop and prev_stop_raw <= s1 and stop > src:
                # crossover(stop, ema): stop > src and stop[1] <= src[1]
                signal = "SELL"

        self.prev_src = src
        self.prev_stop = stop
        return IndicatorPoint(
            time=c.time,
            values={
                "close": c.close,
                "src": src,
                "atr": atr if atr is not None else NAN,
                "trailing_stop": stop,
                "pos": self.pos,
            },
            signal=signal,
        )


class UTBotIndicator(BaseIndicator):
    name = "ut_bot"
    label = "UT Bot Alerts"
    description = "ATR trailing-stop flips (QuantNomad). Buy when price crosses above the trailing stop, sell when it crosses below."
    params = [
        ParamSpec("key_value", "Key Value", "float", 1.0, min=0.1, max=50, step=0.1, description="Sensitivity multiplier on ATR"),
        ParamSpec("atr_period", "ATR Period", "int", 10, min=1, max=500, step=1),
        ParamSpec("heikin_ashi", "Heikin Ashi source", "bool", False),
    ]
    overlay_keys = ["trailing_stop"]

    @classmethod
    def create_stream(cls, settings) -> UTBotStream:
        return UTBotStream(cls.validate_settings(settings))
