# Trading logic

## Indicators

Both indicators are stateful streams (`IndicatorStream.update(candle)`), fed **only closed candles**, oldest first, exactly once each. Historical (batch) results are produced by replaying candles through the same stream, so chart signals and live signals cannot diverge. A stream rejects duplicate or out-of-order candles, so a value can never be computed twice or from the future.

### ATR (shared)

Pine `ta.atr(length)` = `ta.rma(ta.tr(true), length)`:
- True range: `max(high − low, |high − close[1]|, |low − close[1]|)`; on the first bar `high − low`.
- RMA is seeded with the SMA of the first `length` true ranges, then `atr = (atr[1]·(length−1) + tr) / length`.
- `na` (no value) until `length` bars exist. No signal is produced during warm-up.

### UT Bot Alerts (QuantNomad, Pine v4)

Parameters: `key_value` (a, default 1), `atr_period` (c, default 10), `heikin_ashi` (h, default false).

```
xATR  = atr(c);  nLoss = a * xATR
src   = h ? heikin-ashi close ((o+h+l+c)/4) : close
stop  = src > nz(stop[1]) and src[1] > nz(stop[1]) ? max(nz(stop[1]), src − nLoss)
      : src < nz(stop[1]) and src[1] < nz(stop[1]) ? min(nz(stop[1]), src + nLoss)
      : src > nz(stop[1]) ? src − nLoss : src + nLoss
buy   = src > stop and crossover(src, stop)       // ema(src,1) == src
sell  = src < stop and crossunder(src, stop)
```

The ATR always uses the regular candles, as in the original script; Heikin Ashi only changes `src`.

### Supertrend (Pine `ta.supertrend`, used by the reference SuperTrend.pine)

Parameters: `atr_period` (default 10), `multiplier` (default 3.0).

```
upper = hl2 + m*atr;  lower = hl2 − m*atr
lower := lower > nz(lower[1]) or close[1] < nz(lower[1]) ? lower : nz(lower[1])
upper := upper < nz(upper[1]) or close[1] > nz(upper[1]) ? upper : nz(upper[1])
dir   := na(atr[1]) ? 1 : st[1] == upper[1] ? (close > upper ? −1 : 1) : (close < lower ? 1 : −1)
st    := dir == −1 ? lower : upper
BUY on dir 1 → −1 (bearish → bullish), SELL on −1 → 1
```

### Accuracy guarantees and tests

- `tests/test_indicators.py` contains independent, line-by-line Pine transliterations (series semantics with `nz`/`na`). These are separate implementations, not calls into the production code. Production ATR, trailing stop, bands, direction and **signal locations** are asserted equal on synthetic data, including warm-up and trend-flip edge cases.
- `tests/test_real_data_signals.py` repeats the comparison on **real Revolut X candles** (`tests/data/BTC-GBP_1h.json`, `ETH-GBP_15m.json`, ~1000 candles each) for default and non-default parameters. Every BUY/SELL timestamp must match, and a candle-by-candle replay (as in live polling) must equal the batch result.
- No-lookahead test: values and signals for candle *i* are identical whether computed on `candles[:i+1]` or on the full history.
- Signals strictly alternate, at most one per candle.

Signal locations match TradingView's for the same OHLC data. Small differences against a TradingView chart can only come from different input candles (TradingView's Revolut X feed vs the Revolut X API) or from the start of the loaded history, because RMA warm-up converges after roughly 10× the ATR period. The app loads 999 candles.

### Candle close

A candle is closed when `now ≥ candle_start + interval + 2 s`. The forming candle is drawn on the chart but never fed to an indicator, so **signals never repaint**. At a 3 s poll interval, a signal is detected about 2–5 s after the close.

## Signal → order

`TradingEngine.on_signal` runs under one async lock (one decision at a time).

1. **Persist and dedupe.** Insert into `signals` with unique `(config_id, candle_ts, signal_type)`. A duplicate is ignored, including across restarts and repeated polls.
2. **Gates** (SKIPPED with reason if any fails): automation running; no emergency stop; not paused; not blocked by an unknown order; market data not stale; signal not older than 2 candles + 60 s; LIVE connection verified within 10 min.
3. **BUY**
   - Skip if a position on the pair is already open (one position per strategy).
   - Size: fixed quote amount, or `%` of available quote balance.
   - Risk (`trading/risk.py`), in order: max open positions → max daily loss (realised P&L since 00:00 UTC) → cap at max position size → amount > 0 → enough balance → keeps minimum balance → at least the exchange minimum (`min_order_size_quote`).
   - Submit a market buy spending the quote amount (`quote_size`).
4. **SELL**
   - Skip if no open position (no shorting). The position must belong to the active config and mode.
   - Quantity = open quantity; LIVE caps it at the available exchange balance. It is rounded down to the pair step and must meet the exchange minimum.
   - Submit a market sell of the base quantity (`base_size`).
5. **Write-ahead.** The order row (status `CREATED`, fresh UUID `client_order_id`) is committed **before** the adapter is called.
6. **Apply the result.** Actual filled quantity, average price and fees are used, never the requested values.
   - Buy fill → open a trade (or add to it). Base-currency fees reduce the held quantity.
   - Sell fill → reduce `open_quantity`. Realised P&L = proceeds − fees − proportional entry cost; the trade closes when the open quantity reaches zero (dust below the pair minimum counts as closed).
   - Partial fills apply exactly what filled. Rejected or cancelled orders with no fill change nothing.
   - Each order is applied once (`orders.applied`).

Paper and live go through this same code. The only difference is the `ExecutionAdapter` used in step 5:

| | `PaperExecutionAdapter` | `RevolutXExecutionAdapter` |
| --- | --- | --- |
| Fill price | real Revolut X ask (buy) / bid (sell) | exchange fills |
| Fees | `PAPER_FEE_RATE` on notional | exchange `total_fee` (converted to quote if charged in base) |
| Balance | `paper_balances` table | `GET /balances` |
| Network order calls | none, ever | `POST /orders`, then poll `GET /orders/{id}` until terminal (≤15 s), fills from `/orders/fills/{id}` |

## Failures and uncertainty

| Case | Behaviour |
| --- | --- |
| Duplicate signal | Rejected by the unique constraint; no second order. |
| Exchange rejects (4xx before acceptance) | Order `FAILED` or `REJECTED` with reason; no trade; trading continues. |
| Partial fill | Position opened or reduced by the filled quantity; the remainder stays open (for sells) for the next SELL or a manual close. |
| Timeout / connection drop / 5xx after sending `POST /orders` | The request may have reached Revolut X. Order → `UNKNOWN`, **all trading blocked**, and it is **never retried**. The reconcile loop (every 10 s) looks the order up by `client_order_id` in active and historical orders (or by exchange id). Found → its real fills are applied. Confirmed absent after 120 s → `FAILED`. Then the block lifts. |
| Order accepted but not terminal within 15 s | Left `NEW` / `PARTIALLY_FILLED` and finished by the reconcile loop. |
| 429 rate limit | GET: backoff and retry honouring `Retry-After`. POST: treated as not placed (the exchange rejected it), no retry. |
| Stale market data | No new orders until data recovers (status badge STALE). |

## Restart recovery

On startup the engine:
1. Forces **PAPER** mode and stopped automation. Emergency stop, pause and risk limits are restored from `app_state`.
2. Restores the active strategy config and positions from the database.
3. Reconciles every non-final order: paper orders are re-read from the paper ledger; live orders are resolved against Revolut X as above. Anything unresolved keeps trading blocked.

## Live startup reconciliation

`TradingEngine.live_startup_check()` runs when switching to LIVE, on every **Start** in LIVE, and on demand (`POST /api/exchange/reconcile`). LIVE is refused unless all of these pass:

1. Authenticated connection works and the pair is enabled for the account.
2. All local orders with unknown or non-final state are resolved against Revolut X.
3. **No active orders** on the pair at Revolut X, whether placed by this app or by hand. Cancel them or wait for them first.
4. **Fills match.** For every order in the last 7 days whose `client_order_id` is ours, the exchange filled quantity equals the local record.
5. **Balances cover positions.** Every open LIVE position's quantity is available in the exchange balance.

Warnings (they do not block): holdings of the base currency beyond the strategy position, and orders filled outside the app. The bot never sells holdings it did not buy.

The report is shown in the dashboard (*Revolut X connection*) and in `GET /api/trading/status` → `live_check`.

## Controls

- **Start / Stop** turns automated execution on or off. Signals are still computed and recorded while stopped (as SKIPPED).
- **Pause / Resume** is like stop but persists across restarts, for temporary holds.
- **Emergency stop** immediately blocks all new automated orders, buys and sells, and persists across restarts until **Reset**. Open positions are **left as they are**: they are not liquidated. Close them manually with **Close** in the Positions tab (allowed during an emergency stop) or in Revolut X. A market order cannot be cancelled once sent; an order in flight finishes and is recorded.
- **Mode** can only change while stopped. PAPER → LIVE requires the phrase plus a passing reconciliation; LIVE → PAPER is always allowed. Positions are tracked per mode.
- Strategy (pair, timeframe, indicator, parameters, sizing) can only change while stopped. Changing pair or timeframe reloads history and recalculates.

## Tests

`cd backend && pytest -q`:
- `test_indicators.py`, `test_real_data_signals.py`: ATR, UT Bot trailing stop and signals, Supertrend bands, direction and flips, Pine reference equivalence on synthetic and real data, batch vs incremental, no lookahead, no duplicates, warm-up, parameter validation.
- `test_engine.py`: paper round trip with fees and P&L, duplicate signals, sell without position, insufficient balance, risk limits, emergency stop, pause, restart recovery, manual close, live confirmation, rejection, partial fills, timeout → UNKNOWN → no retry → reconciliation, balance-capped sells, live startup reconciliation (open orders, fill mismatch, uncovered positions, unknown orders, warnings).
- `test_revolut_x.py`: signatures byte-identical to the official TypeScript client (Node `crypto.sign`), headers, query and body canonicalisation, order request construction, public and balance mocks, status parsing, 4xx/429/5xx mapping, retry policy (no POST retry), timeouts.
- `test_paper_and_market.py`: paper fills and fees, sizing, candle close detection, signal dedup across polls.

All network calls are mocked; no test can place a real order.
