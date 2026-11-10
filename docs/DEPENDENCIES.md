# Dependencies

## Reference repositories (git submodules under `vendor/`)

| Repository | Pinned commit | Licence | How it is used |
| --- | --- | --- | --- |
| [tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts) | `0bad38a070220e6315aa779bb80483fe97cf6d1b` (v5.3.0) | Apache-2.0 | Charting library. The frontend uses the identical published npm package `lightweight-charts@5.3.0`; the submodule is the source reference. See attribution below. |
| [revolut-engineering/revolut-x-api](https://github.com/revolut-engineering/revolut-x-api) | `224637499d0cb67ced2b78dcf2e48b1b8dc7ee7f` (v1.0.53+1) | MIT | Official API reference: endpoints, payloads, error codes, rate limits, Ed25519 signing. The Python client in `backend/app/exchanges/revolut_x.py` is a new implementation; signatures are tested to be byte-identical to this repo's TypeScript signer. |
| [sadratech/SuperTrend-Indicator](https://github.com/sadratech/SuperTrend-Indicator) | `1b3b022cbed0a614b43c7c74ddd543f8514c94b1` | MIT | Pine reference for Supertrend (it calls `ta.supertrend`). The algorithm is reimplemented in Python. |
| [Aksee123/Ut_bot_ADV_Strategy](https://github.com/Aksee123/Ut_bot_ADV_Strategy) | `aa8962d02f441d5c5d1c95525d7b246d5ae3a51e` | MIT (stated in README) | UT Bot reference for cross-checking. |
| [pinarocko346-creator/ut_bot](https://github.com/pinarocko346-creator/ut_bot) | `0c648d0b7bbb55766eab9da0a27e3f2c00b7c680` | **No licence file** | Read-only reference; **no code copied**. UT Bot is implemented from the original public QuantNomad Pine script logic ([UT Bot Alerts](https://www.tradingview.com/script/n8ss8BID-UT-Bot-Alerts/)). |

Fetch them with `git submodule update --init --recursive`. They are not needed to build or run the app.

## Backend (Python 3.12, `backend/requirements*.txt`, pinned)

| Package | Version | Licence | Use |
| --- | --- | --- | --- |
| fastapi | 0.115.12 | MIT | REST and WebSocket API |
| uvicorn[standard] | 0.34.2 | BSD-3 | ASGI server |
| pydantic / pydantic-settings | 2.11.4 / 2.9.1 | MIT | Validation, settings |
| httpx | 0.28.1 | BSD-3 | Async HTTP client |
| pandas / numpy | 2.2.3 / 2.2.5 | BSD-3 | Indicator batch frames |
| SQLAlchemy[asyncio] | 2.0.40 | MIT | ORM |
| asyncpg | 0.30.0 | Apache-2.0 | PostgreSQL driver |
| alembic | 1.15.2 | MIT | Migrations |
| cryptography | 44.0.3 | Apache-2.0 / BSD | Ed25519 signing |
| python-dotenv | 1.1.0 | BSD-3 | `.env` loading |
| pytest, pytest-asyncio, aiosqlite, respx (dev) | 8.3.5, 0.26.0, 0.21.0, 0.22.0 | MIT / Apache-2.0 / MIT / BSD-3 | Tests |

## Frontend (`frontend/package.json`, exact versions)

| Package | Version | Licence | Use |
| --- | --- | --- | --- |
| lightweight-charts | 5.3.0 | Apache-2.0 | Chart |
| react / react-dom | 18.3.1 | MIT | UI |
| vite, @vitejs/plugin-react | 6.3.5, 4.4.1 | MIT | Dev server, build |
| tailwindcss, @tailwindcss/vite | 4.1.7 | MIT | Styling |
| typescript | 5.8.3 | Apache-2.0 | Types |

## Infrastructure

PostgreSQL 16 (`postgres:16-alpine`, PostgreSQL Licence) via `docker-compose.yml`.

## Attribution

TradingView Lightweight Charts™ — Copyright (c) 2025 TradingView, Inc. Licensed under the Apache License, Version 2.0. The upstream `NOTICE` file (`vendor/lightweight-charts/NOTICE`) requires a link to https://www.tradingview.com on any page using the library. The dashboard chart shows the default TradingView attribution logo (`attributionLogo` is left enabled).
