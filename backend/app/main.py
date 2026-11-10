import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router, ws_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database.session import Database
from app.exchanges.revolut_x import RevolutXClient, load_private_key
from app.services.market_data import MarketDataService
from app.trading.engine import TradingEngine
from app.trading.execution import RevolutXExecutionAdapter
from app.trading.paper import PaperExecutionAdapter
from app.websocket.manager import Broadcaster

logger = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[1]


def run_migrations(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def build_live_client(settings: Settings) -> tuple[RevolutXClient | None, str | None]:
    if not settings.credentials_configured:
        return None, None
    try:
        key = load_private_key(settings.revolut_x_private_key_path)
    except Exception as exc:
        logger.error("Could not load Revolut X private key: %s", exc)
        return None, f"Private key error: {exc}"
    client = RevolutXClient(
        base_url=settings.revolut_x_base_url,
        api_key=settings.revolut_x_api_key,
        private_key=key,
        region=settings.revolut_x_region,
        timeout=settings.revolut_x_timeout,
    )
    return client, None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    await asyncio.to_thread(run_migrations, settings.database_url)

    db = Database(settings.database_url)
    broadcaster = Broadcaster()
    public_client = RevolutXClient(base_url=settings.revolut_x_base_url, region=settings.revolut_x_region, timeout=settings.revolut_x_timeout)
    live_client, key_error = build_live_client(settings)
    market = MarketDataService(public_client, settings, broadcaster)
    paper = PaperExecutionAdapter(db, market.get_pair, market.get_quote, settings.paper_fee_rate, settings.paper_starting_balance)
    live = (
        RevolutXExecutionAdapter(live_client, market.get_pair, settings.order_status_poll_interval, settings.order_status_timeout)
        if live_client
        else None
    )
    engine = TradingEngine(db, settings, market, broadcaster, paper, live, live_client)
    if key_error:
        engine.connection_error = key_error
    await engine.startup()
    app.state.engine = engine

    stop = asyncio.Event()
    tasks = [asyncio.create_task(market.run(stop), name="market"), asyncio.create_task(engine.reconcile_loop(stop), name="reconcile")]
    logger.info("Crypto terminal started in %s mode (automated trading stopped)", engine.mode.value)
    try:
        yield
    finally:
        logger.info("Shutting down")
        engine.running = False
        stop.set()
        await asyncio.wait(tasks, timeout=10)
        for t in tasks:
            t.cancel()
        await public_client.close()
        if live_client:
            await live_client.close()
        await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Crypto Trading Terminal", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(ws_router)
    return app


app = create_app()
