# Crypto Terminal

A minimal, locally hosted automated crypto trading terminal for the **Revolut X** exchange.

- Candlestick + volume chart (TradingView Lightweight Charts) with indicator overlay and BUY/SELL, entry/exit markers.
- Indicators: **UT Bot Alerts** and **Supertrend**, reproducing the original TradingView Pine Script calculations, confirmed on candle close (no repainting).
- Automated execution in **PAPER** (default) or **LIVE** mode through one shared trading engine; only the execution adapter differs.
- Risk controls: fixed or %-of-balance sizing, max position size, max daily loss, max open positions, minimum balance, pause/resume, emergency stop.
- Persists configuration, signals, orders, trades, fees and P&L in PostgreSQL; recovers and reconciles with the exchange after a restart.

> This is not a TradingView competitor. It is intentionally small: one pair, one strategy, one user, on your machine.

**Safety defaults.** The app always starts in PAPER mode with automated trading stopped. LIVE mode requires API credentials, a successful exchange reconciliation and typing the phrase `ENABLE LIVE TRADING`. Nothing in the test suite or paper mode can send a real order.

## Requirements

| Tool | Version |
| --- | --- |
| Python | 3.12+ |
| Node.js | 20+ (tested with 22) |
| Docker (for PostgreSQL) | any recent version |

## Quick start

```bash
git clone --recurse-submodules <this repo> crypto-terminal && cd crypto-terminal
cp .env.example .env                     # PAPER mode works without credentials

# 1. Database (PostgreSQL 16 on localhost:5433)
docker compose up -d db

# 2. Backend (migrations run automatically on startup)
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Frontend (new terminal)
cd frontend
npm ci
npm run dev                               # http://127.0.0.1:5173
```

Open http://127.0.0.1:5173. The header shows the pair, price, execution mode, automation state, exchange connection and a **Data LIVE / STALE / DISCONNECTED** badge.

To trade on paper: choose pair, timeframe and indicator in *Strategy*, set a size, click **Apply strategy**, then **Start**. Orders are filled at the real Revolut X bid/ask with a simulated fee (`PAPER_FEE_RATE`, default 0.09%) against a simulated balance (`PAPER_STARTING_BALANCE`). `POST /api/paper/reset` resets the paper account.

For live trading follow [docs/REVOLUT_X_SETUP.md](docs/REVOLUT_X_SETUP.md) (API key, private key, **IP allow-listing** and connection checks).

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `REVOLUT_X_API_KEY` | empty | 64-char API key. Empty = paper only. |
| `REVOLUT_X_PRIVATE_KEY_PATH` | empty | Path to the Ed25519 private key (PEM). Must be `chmod 600`. |
| `REVOLUT_X_BASE_URL` | `https://revx.revolut.com` | API host. |
| `REVOLUT_X_REGION` | `UK` | Region used for public market data. |
| `DATABASE_URL` | `postgresql+asyncpg://crypto:crypto@localhost:5433/crypto_terminal` | Database. |
| `MARKET_POLL_INTERVAL` | `3` | Seconds between market-data poll rounds (min 1.1). |
| `STALE_DATA_SECONDS` | `30` | No successful poll for this long = data STALE, no new orders. |
| `PAPER_STARTING_BALANCE` | `10000` | Paper quote balance. |
| `PAPER_FEE_RATE` | `0.0009` | Paper taker fee. |
| `BACKEND_HOST` / `BACKEND_PORT` | `127.0.0.1` / `8000` | Backend bind. Keep it on localhost: the API has no auth. |

## Tests

```bash
cd backend && source .venv/bin/activate && pytest -q      # 86 tests, no network, no real orders
cd frontend && npm run build                              # type check + production build
```

Tests use a throwaway PostgreSQL database when `docker compose` is running (otherwise SQLite) and mocked Revolut X responses. See [docs/TRADING_LOGIC.md](docs/TRADING_LOGIC.md#tests) for what is covered.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, REST and WebSocket API.
- [docs/TRADING_LOGIC.md](docs/TRADING_LOGIC.md): indicators, signal rules, execution, risk, reconciliation, emergency stop.
- [docs/REVOLUT_X_SETUP.md](docs/REVOLUT_X_SETUP.md): credentials, IP allow-listing, verifying the connection, going live.
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md): external repositories, versions, licences, attribution.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Backend unavailable` in the browser | Backend not running on :8000, or database not up (`docker compose ps`). |
| Data badge **STALE** | Revolut X polls are failing or slow; check the badge tooltip and backend log. Trading resumes automatically when data recovers. |
| Data badge **DISCONNECTED** | No successful poll yet or the browser lost its WebSocket to the backend. |
| `Lookup window interval exceeds the limit of 1000 candles` | Revolut X returns at most 1000 candles per request; the app requests 999. |
| Exchange `AUTH_FAILED` with 403 | Your public IP is not on the API key's allow-list. See [IP allow-listing](docs/REVOLUT_X_SETUP.md#3-allow-list-your-ip-address). |
| Exchange `AUTH_FAILED` with 401 | Wrong API key or private key, or the machine clock is off (timestamps are checked). |
| `Private key ... permissions` error | `chmod 600 <key file>`. |
| Trading blocked: `live order(s) with unknown outcome` | An order timed out. The app is checking Revolut X for it; it unblocks once the order is found or confirmed absent. Never place it again manually without checking Revolut X first. |
