"""Signal-location regression on real Revolut X candles.

``tests/data/*.json`` are snapshots of the public candles endpoint. For each snapshot the
production indicators must place BUY/SELL signals on exactly the same candle timestamps as the
independent Pine transliterations in ``test_indicators`` (series semantics of the TradingView
scripts), and replaying the candles one by one (as the live poller does) must agree with batch.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from app.indicators.base import Candle
from app.indicators.supertrend import SupertrendIndicator
from app.indicators.ut_bot import UTBotIndicator
from tests.test_indicators import _pine_supertrend, _pine_ut_bot

DATA = sorted((Path(__file__).parent / "data").glob("*.json"))


def load(path):
    raw = json.loads(path.read_text())["candles"]
    df = pd.DataFrame(
        {
            "time": [int(c["start"]) for c in raw],
            "open": [float(c["open"]) for c in raw],
            "high": [float(c["high"]) for c in raw],
            "low": [float(c["low"]) for c in raw],
            "close": [float(c["close"]) for c in raw],
            "volume": [float(c["volume"]) for c in raw],
        }
    )
    return df.sort_values("time").reset_index(drop=True)


def times(df, flags):
    return [int(t) for t, f in zip(df.time, flags) if f]


@pytest.mark.parametrize("path", DATA, ids=lambda p: p.stem)
@pytest.mark.parametrize("params", [dict(key_value=1.0, atr_period=10, heikin_ashi=False), dict(key_value=2.0, atr_period=14, heikin_ashi=True)])
def test_ut_bot_signal_timestamps_match_pine(path, params):
    df = load(path)
    res = UTBotIndicator.calculate(df, params)
    _, buys, sells = _pine_ut_bot(df, params["key_value"], params["atr_period"], params["heikin_ashi"])
    assert times(df, res.signal == "BUY") == times(df, buys)
    assert times(df, res.signal == "SELL") == times(df, sells)
    assert sum(buys) > 0 and sum(sells) > 0


@pytest.mark.parametrize("path", DATA, ids=lambda p: p.stem)
@pytest.mark.parametrize("params", [dict(atr_period=10, multiplier=3.0), dict(atr_period=7, multiplier=2.0)])
def test_supertrend_signal_timestamps_match_pine(path, params):
    df = load(path)
    res = SupertrendIndicator.calculate(df, params)
    _, _, buys, sells = _pine_supertrend(df, params["multiplier"], params["atr_period"])
    assert times(df, res.signal == "BUY") == times(df, buys)
    assert times(df, res.signal == "SELL") == times(df, sells)
    assert sum(buys) > 0 and sum(sells) > 0


@pytest.mark.parametrize("path", DATA, ids=lambda p: p.stem)
@pytest.mark.parametrize("ind", [UTBotIndicator, SupertrendIndicator])
def test_live_replay_matches_batch_on_real_data(path, ind):
    df = load(path)
    batch = ind.calculate(df)
    stream = ind.create_stream(ind.default_settings())
    live = [stream.update(Candle(int(r.time), r.open, r.high, r.low, r.close, r.volume)).signal for r in df.itertuples()]
    assert live == batch.signal.tolist()
