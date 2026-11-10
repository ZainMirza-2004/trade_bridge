# Architecture

```
┌──────────────────────── browser (React + Vite) ──────────────────────────┐
│ Chart (lightweight-charts) · ControlPanel · TradesPanel · DataBadge       │
└──────────────┬──────────────────────────────────▲────────────────────────┘
          REST /api/*                       WebSocket /ws (events)
┌──────────────▼──────────────────────────────────┴────────────────────────┐
│ FastAPI (backend/app)                                                     │
│  api/routes.py ── TradingEngine (trading/engine.py) ── Broadcaster (ws)   │
│                    │        │            │                                │
│        MarketDataService   RiskLimits   ExecutionAdapter                  │
│        (services/market_   (risk.py)    ├─ PaperExecutionAdapter (paper)  │
│         data.py)                        └─ RevolutXExecutionAdapter (live)│
│            │  IndicatorStream (indicators/*)          │                   │
│            └────────── RevolutXClient (exchanges/revolut_x.py) ──────────┼──► revx.revolut.com
│  SQLAlchemy async ──► PostgreSQL (signals, orders, trades, config, state) │
└───────────────────────────────────────────────────────────────────────────┘
```

## Backend modules

| Module | Responsibility |
| --- | --- |
| `core/config.py` | Settings from `.env` (pydantic-settings). |
| `core/enums.py`, `core/decimal_utils.py`, `core/timeframes.py` | Shared enums, `Decimal` helpers, timeframe ↔ minutes. |
| `database/models.py` | `TradingConfig`, `AppState`, `Signal`, `Order`, `Trade`, `PaperBalance`. Money columns are `Numeric(36, 18)`. Migrations live in `alembic/` and run on startup. |
| `exchanges/revolut_x.py` | Async Revolut X REST client: Ed25519 signing, per-endpoint rate limiting, error mapping, safe retries (GET only), order request construction. |
| `indicators/` | `BaseIndicator` / `IndicatorStream` interface, `WilderATR`, `UTBotIndicator`, `SupertrendIndicator`, registry. |
| `services/market_data.py` | Polls ticker and candles, separates closed from forming candles, feeds closed candles to the indicator stream, emits signals, tracks data health (LIVE/STALE/DISCONNECTED). |
| `trading/engine.py` | The only place trading decisions are made: signal persistence and dedup, gates, risk, sizing, order write-ahead, position and P&L accounting, reconciliation, controls. |
| `trading/execution.py` | `ExecutionAdapter` interface, `OrderResult`, live Revolut X adapter. |
| `trading/paper.py` | Paper adapter: fills at real bid/ask, simulated fee, persisted balances. |
| `trading/risk.py` | Position sizing and entry risk checks (pure functions). |
| `websocket/manager.py` | Fan-out of JSON events `{type, data}` to connected browsers. |
| `api/routes.py` | REST and WebSocket endpoints. |
| `main.py` | App factory and lifespan: migrations, clients, engine startup and reconciliation, background tasks. |

### Adding an indicator

Implement `BaseIndicator` (`name`, `label`, `params()` with `ParamSpec`s, `create_stream()` returning an `IndicatorStream` whose `update(candle)` returns an `IndicatorPoint` with an optional `"BUY"`/`"SELL"` signal), then register it in `indicators/registry.py`. The engine, API and UI (parameter form, overlay line from `overlay_keys`) pick it up without changes.

## Data flow

1. `MarketDataService.run` loop, every `MARKET_POLL_INTERVAL` seconds: `poll_ticker` (→ `market_update`), then `poll_candles`, which fetches the last few candles and merges them (→ `candle_update` for the forming candle).
2. A candle counts as **closed** only when `now ≥ start + interval + 2 s grace`. Each newly closed candle is fed once, in order, to the stateful indicator stream. Batch history uses the same stream, so live and historical values are identical.
3. A signal on a closed candle → `indicator_signal` event plus `TradingEngine.on_signal`.
4. The engine persists the signal (unique per config, candle and type), applies gates and risk, writes the order row **before** calling the adapter, then applies the fill result to positions → `order_update`, `trade_opened` / `trade_closed` / `trade_updated`, `balance_update`, `trading_status`.
5. Data health changes (LIVE ↔ STALE ↔ DISCONNECTED) broadcast `trading_status`.

## REST API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness, data status, mode. |
| GET | `/api/exchange/status` | Public data and authenticated connection state. |
| POST | `/api/exchange/test-connection` | Authenticated read-only check (balances + pairs). |
| POST | `/api/exchange/reconcile` | Run the live startup reconciliation (read-only). |
| GET | `/api/exchange/reconciliation` | Last reconciliation report. |
| GET | `/api/exchange/public-ip` | This machine's public IPv4 (for allow-listing). |
| GET | `/api/exchange/balances` | Real Revolut X balances. |
| GET | `/api/exchange/pairs` | Tradeable pairs (dynamic). |
| GET | `/api/market/candles` | Active chart: candles, indicator points, historical signals. `?symbol&timeframe` for an ad-hoc chart. |
| GET | `/api/market/ticker`, `/api/market/timeframes` | Latest ticker; supported timeframes. |
| GET | `/api/indicators` | Available indicators, their parameters, and the active one. |
| POST | `/api/indicators/configure` | Set pair, timeframe, indicator, parameters, sizing (only while stopped). |
| GET | `/api/indicators/signals` | Chart signals plus recorded signals with execution status. |
| GET | `/api/trading/status` | Full engine status (the `trading_status` event payload). |
| POST | `/api/trading/start` · `/stop` · `/pause` · `/resume` | Automation controls. |
| POST | `/api/trading/mode` | `{"mode": "PAPER"}` or `{"mode": "LIVE", "confirmation": "ENABLE LIVE TRADING"}`. |
| GET | `/api/trading/orders`, `/api/trading/balances` | Orders; balances for the current mode. |
| GET | `/api/trades`, `/api/trades/open`, `/api/trades/history` | Positions and round trips. |
| POST | `/api/trades/{id}/close` | Manually close a strategy position (market sell). |
| GET | `/api/performance` | P&L, win rate, profit factor, drawdown, fees. |
| POST | `/api/risk/configure` | Risk limits. |
| POST | `/api/emergency-stop`, `/api/emergency-stop/reset` | Emergency stop. |
| GET/POST | `/api/paper/balances`, `/api/paper/reset` | Paper account. |

## WebSocket events (`/ws`)

`market_update`, `candle_update`, `indicator_signal`, `order_update`, `trade_opened`, `trade_closed`, `trade_updated`, `balance_update`, `trading_status`. Each message is `{"type": ..., "data": ...}`. The browser reconnects automatically and re-fetches state after reconnecting.

## Frontend

`frontend/src/App.tsx` owns the state (REST on load and after events, WebSocket for pushes). `components/Chart.tsx` renders candles, volume, the indicator overlay, signal markers and trade entry/exit markers, and updates the forming candle in place. `components/ControlPanel.tsx` holds every control; `components/TradesPanel.tsx` has the Positions, History, Signals and Orders tabs. Vite proxies `/api` and `/ws` to the backend in development.
