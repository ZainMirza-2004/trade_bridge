"""Indicator tests.

Reference values come from an independent, array-based transliteration of the Pine scripts
(``_pine_*`` below — series semantics with ``[1]`` look-backs and ``nz``/``na`` rules) and from
small hand-computed fixtures. The production code is an incremental state machine, so agreement
between the two validates both the maths and the absence of lookahead.
"""

import math

import numpy as np
import pandas as pd
import pytest

from app.indicators.base import Candle, WilderATR
from app.indicators.registry import INDICATORS, get_indicator
from app.indicators.supertrend import SupertrendIndicator
from app.indicators.ut_bot import UTBotIndicator


def make_candles(n=400, seed=7, start=1_700_000_000_000, step=60_000):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.2, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.random(n) * 1.5
    low = np.minimum(open_, close) - rng.random(n) * 1.5
    return pd.DataFrame(
        {"time": start + np.arange(n) * step, "open": open_, "high": high, "low": low, "close": close, "volume": rng.random(n)}
    )


# ----------------------------------------------------------------- independent Pine references


def _na(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def _pine_atr(h, l, c, length):
    n = len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    out = [math.nan] * n
    for i in range(n):
        if i == length - 1:
            out[i] = sum(tr[:length]) / length
        elif i >= length:
            out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def _pine_ut_bot(df, a=1.0, c=10, ha=False):
    o, h, l, cl = (df[k].tolist() for k in ("open", "high", "low", "close"))
    src = [(o[i] + h[i] + l[i] + cl[i]) / 4 for i in range(len(cl))] if ha else cl
    atr = _pine_atr(h, l, cl, c)
    stop, buys, sells = [], [], []
    for i in range(len(cl)):
        nloss = a * atr[i]
        prev = stop[i - 1] if i > 0 else math.nan
        p = 0.0 if _na(prev) else prev
        s1 = src[i - 1] if i > 0 else math.nan
        def gt(x, y): return not _na(x) and not _na(y) and x > y
        def lt(x, y): return not _na(x) and not _na(y) and x < y
        if gt(src[i], p) and gt(s1, p):
            v = math.nan if _na(nloss) else max(p, src[i] - nloss)
        elif lt(src[i], p) and lt(s1, p):
            v = math.nan if _na(nloss) else min(p, src[i] + nloss)
        elif gt(src[i], p):
            v = src[i] - nloss
        else:
            v = src[i] + nloss
        stop.append(v)
        # crossover(x, y): x > y and x[1] <= y[1]
        buys.append(i > 0 and gt(src[i], v) and not _na(prev) and not _na(s1) and s1 <= prev)
        sells.append(i > 0 and gt(v, src[i]) and not _na(prev) and not _na(s1) and prev <= s1)
    return stop, buys, sells


def _pine_supertrend(df, factor=3.0, length=10):
    h, l, cl = (df[k].tolist() for k in ("high", "low", "close"))
    atr = _pine_atr(h, l, cl, length)
    up, lo, st, dr = [], [], [], []
    for i in range(len(cl)):
        hl2 = (h[i] + l[i]) / 2
        u, d = hl2 + factor * atr[i], hl2 - factor * atr[i]
        pl = 0.0 if i == 0 or _na(lo[i - 1]) else lo[i - 1]
        pu = 0.0 if i == 0 or _na(up[i - 1]) else up[i - 1]
        c1 = cl[i - 1] if i > 0 else math.nan
        d = d if ((not _na(d) and d > pl) or (not _na(c1) and c1 < pl)) else pl
        u = u if ((not _na(u) and u < pu) or (not _na(c1) and c1 > pu)) else pu
        if i == 0 or _na(atr[i - 1]):
            direction = 1
        elif st[i - 1] == up[i - 1]:
            direction = -1 if cl[i] > u else 1
        else:
            direction = 1 if cl[i] < d else -1
        up.append(u); lo.append(d); dr.append(direction)
        st.append(d if direction == -1 else u)
    buys = [i > 0 and dr[i] < 0 and dr[i - 1] > 0 for i in range(len(cl))]
    sells = [i > 0 and dr[i] > 0 and dr[i - 1] < 0 for i in range(len(cl))]
    return st, dr, buys, sells


# ----------------------------------------------------------------- ATR


def test_wilder_atr_hand_computed():
    atr = WilderATR(3)
    bars = [(10, 8, 9), (11, 9, 10), (12, 10, 11), (11, 7, 8)]
    out = [atr.update(*b) for b in bars]
    # TR: 2, max(2,2,0)=2, max(2,2,0)=2, max(4,0,4)=4
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx((2.0 * 2 + 4) / 3)


@pytest.mark.parametrize("length", [1, 5, 14])
def test_atr_matches_pine_reference(length):
    df = make_candles(200)
    atr = WilderATR(length)
    got = [atr.update(h, l, c) for h, l, c in zip(df.high, df.low, df.close)]
    ref = _pine_atr(df.high.tolist(), df.low.tolist(), df.close.tolist(), length)
    for g, r in zip(got, ref):
        assert (g is None and math.isnan(r)) or g == pytest.approx(r, rel=1e-12)


# ----------------------------------------------------------------- UT Bot


@pytest.mark.parametrize("params", [dict(key_value=1, atr_period=10, heikin_ashi=False),
                                    dict(key_value=2.5, atr_period=5, heikin_ashi=False),
                                    dict(key_value=1, atr_period=10, heikin_ashi=True)])
def test_ut_bot_matches_pine_reference(params):
    df = make_candles(500, seed=11)
    res = UTBotIndicator.calculate(df, params)
    stop, buys, sells = _pine_ut_bot(df, params["key_value"], params["atr_period"], params["heikin_ashi"])
    np.testing.assert_allclose(res["trailing_stop"].to_numpy(), np.array(stop), rtol=1e-12, equal_nan=True)
    assert (res["signal"] == "BUY").tolist() == buys
    assert (res["signal"] == "SELL").tolist() == sells
    assert sum(buys) > 3 and sum(sells) > 3


def test_ut_bot_trailing_stop_hand_computed():
    # ATR period 1 -> ATR == TR; key 1.
    candles = [Candle(i, *ohlc) for i, ohlc in enumerate([(10, 11, 9, 10), (10, 12, 10, 12), (12, 13, 11, 12.5), (12.5, 12.5, 8, 9)])]
    res = UTBotIndicator.calculate(candles, dict(key_value=1, atr_period=1))
    # bar0: TR=2, prev stop nz=0, src(10)>0 but src[1] na -> src-nLoss=8
    # bar1: TR=max(2,2,0)=2; 12>8 and 10>8 -> max(8, 10)=10  => BUY? src[1]=10 <= prev 8? no -> no buy
    # bar2: TR=max(2,1,1)=2; max(10, 10.5)=10.5
    # bar3: TR=max(4.5,0.5,4.5)=4.5; 9<10.5 and 12.5>10.5 -> src+nLoss = 13.5 ; crossover(stop, src): 13.5>9 and 10.5<=12.5 -> SELL
    assert res["trailing_stop"].tolist() == pytest.approx([8, 10, 10.5, 13.5])
    assert res["signal"].tolist() == [None, None, None, "SELL"]


def test_ut_bot_signals_alternate_and_no_duplicates():
    res = UTBotIndicator.calculate(make_candles(800, seed=3))
    sigs = res["signal"].dropna().tolist()
    assert all(a != b for a, b in zip(sigs, sigs[1:])), "consecutive identical signals"


# ----------------------------------------------------------------- Supertrend


@pytest.mark.parametrize("params", [dict(atr_period=10, multiplier=3.0), dict(atr_period=7, multiplier=1.5)])
def test_supertrend_matches_pine_reference(params):
    df = make_candles(600, seed=5)
    res = SupertrendIndicator.calculate(df, params)
    st, dr, buys, sells = _pine_supertrend(df, params["multiplier"], params["atr_period"])
    valid = ~res["atr"].isna()
    np.testing.assert_allclose(res["supertrend"][valid].to_numpy(), np.array(st)[valid.to_numpy()], rtol=1e-12)
    assert res["direction"].tolist() == dr
    assert (res["signal"] == "BUY").tolist() == buys
    assert (res["signal"] == "SELL").tolist() == sells
    assert sum(buys) >= 2 and sum(sells) >= 2


def test_supertrend_trend_changes_on_clear_reversal():
    up = [Candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
    down = [Candle(30 + i, 129 - 3 * i, 130 - 3 * i, 126 - 3 * i, 127 - 3 * i) for i in range(30)]
    res = SupertrendIndicator.calculate(up + down, dict(atr_period=5, multiplier=2))
    assert res["direction"].iloc[25] == -1  # bullish during the rally
    assert res["direction"].iloc[-1] == 1  # bearish after the sell-off
    sigs = res["signal"].dropna().tolist()
    assert sigs[-1] == "SELL"
    assert all(a != b for a, b in zip(sigs, sigs[1:]))


# ----------------------------------------------------------------- shared properties


@pytest.mark.parametrize("name", list(INDICATORS))
def test_incremental_equals_batch(name):
    ind = get_indicator(name)
    df = make_candles(300, seed=9)
    batch = ind.calculate(df)
    stream = ind.create_stream(ind.default_settings())
    rows = [stream.update(Candle(int(r.time), r.open, r.high, r.low, r.close, r.volume)) for r in df.itertuples()]
    assert [p.signal for p in rows] == batch["signal"].tolist()
    key = ind.overlay_keys[0]
    np.testing.assert_allclose([p.values[key] for p in rows], batch[key].to_numpy(), equal_nan=True)


@pytest.mark.parametrize("name", list(INDICATORS))
def test_no_lookahead(name):
    """Values for candle i must not change when future candles are appended."""
    ind = get_indicator(name)
    df = make_candles(400, seed=21)
    full = ind.calculate(df)
    for cut in (50, 137, 260, 399):
        partial = ind.calculate(df.iloc[:cut])
        assert partial["signal"].tolist() == full["signal"].iloc[:cut].tolist()
        key = ind.overlay_keys[0]
        np.testing.assert_allclose(partial[key].to_numpy(), full[key].iloc[:cut].to_numpy(), equal_nan=True)


@pytest.mark.parametrize("name", list(INDICATORS))
def test_insufficient_history_produces_no_signals(name):
    ind = get_indicator(name)
    res = ind.calculate(make_candles(5))
    assert res["signal"].isna().all()
    assert ind.calculate(make_candles(5).iloc[:0]).empty


@pytest.mark.parametrize("name", list(INDICATORS))
def test_stream_rejects_out_of_order_or_duplicate_candles(name):
    ind = get_indicator(name)
    stream = ind.create_stream(ind.default_settings())
    stream.update(Candle(2, 1, 2, 0.5, 1.5))
    with pytest.raises(ValueError):
        stream.update(Candle(2, 1, 2, 0.5, 1.5))


def test_generate_signals_standard_format():
    df = make_candles(400, seed=11)
    data = UTBotIndicator.calculate(df)
    sigs = UTBotIndicator.generate_signals(data, "BTC-GBP", "1m")
    assert sigs and sigs[0].indicator == "ut_bot" and sigs[0].confirmed
    assert sigs[0].symbol == "BTC-GBP" and sigs[0].signal_type in ("BUY", "SELL")
    assert sigs[0].parameters == {"key_value": 1.0, "atr_period": 10, "heikin_ashi": False}


def test_parameter_validation():
    assert UTBotIndicator.validate_settings({"key_value": "2"})["key_value"] == 2.0
    with pytest.raises(ValueError):
        UTBotIndicator.validate_settings({"atr_period": 0})
    with pytest.raises(ValueError):
        SupertrendIndicator.validate_settings({"bogus": 1})
    with pytest.raises(ValueError):
        UTBotIndicator.validate_settings({"atr_period": 2.5})
