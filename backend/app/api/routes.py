from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core.enums import ExecutionMode
from app.core.timeframes import TIMEFRAMES, interval_minutes
from app.exchanges.revolut_x import RevolutXError
from app.indicators.registry import INDICATORS, get_indicator
from app.services.market_data import parse_candle, candle_dict
from app.trading.engine import TradingEngine, TradingError, dec

router = APIRouter(prefix="/api")
ws_router = APIRouter()


def engine(request: Request) -> TradingEngine:
    return request.app.state.engine


def guard(exc: Exception) -> HTTPException:
    if isinstance(exc, (TradingError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RevolutXError):
        return HTTPException(status_code=502, detail=f"Revolut X: {exc}")
    raise exc



# ---------------------------------------------------------------- health / exchange


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    e = engine(request)
    return {
        "status": "ok",
        "market_data_stale": e.market.is_stale(),
        "market_data_status": e.market.data_status(),
        "mode": e.mode.value,
        "running": e.running,
    }


@router.get("/exchange/status")
async def exchange_status(request: Request) -> dict[str, Any]:
    e = engine(request)
    return {
        "public_api": "ok" if not e.market.is_stale() else "stale",
        "market_data_status": e.market.data_status(),
        "market_data_error": e.market.last_error,
        "credentials_configured": e.live_client is not None,
        "connection_status": e.connection_status.value,
        "connection_error": e.connection_error,
        "live_ready": e.live_ready(),
        "polling_interval_seconds": e.settings.market_poll_interval,
        "region": e.settings.revolut_x_region,
    }


@router.post("/exchange/test-connection")
async def test_connection(request: Request) -> dict[str, Any]:
    try:
        return await engine(request).verify_connection()
    except Exception as exc:
        raise guard(exc)


@router.post("/exchange/reconcile")
async def reconcile_exchange(request: Request) -> dict[str, Any]:
    """Run the live startup reconciliation on demand (read-only: never places or cancels orders)."""
    e = engine(request)
    async with e._lock:
        return await e.live_startup_check()


@router.get("/exchange/reconciliation")
async def last_reconciliation(request: Request) -> dict[str, Any] | None:
    return engine(request).live_check


@router.get("/exchange/public-ip")
async def public_ip() -> dict[str, Any]:
    """The IPv4 address Revolut X sees for this machine (the one to allow-list on the API key)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.ipify.org", params={"format": "json"})
            r.raise_for_status()
            return {"ip": r.json()["ip"]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not determine public IP: {exc}")


@router.get("/exchange/balances")
async def exchange_balances(request: Request) -> dict[str, Any]:
    e = engine(request)
    if e.live is None:
        raise HTTPException(status_code=400, detail="Revolut X API credentials not configured")
    try:
        balances = await e.live.get_balance()
    except Exception as exc:
        raise guard(exc)
    return {"balances": {k: dec(v) for k, v in balances.items()}}


@router.get("/exchange/pairs")
async def exchange_pairs(request: Request) -> list[dict[str, Any]]:
    try:
        pairs = await engine(request).market.load_pairs()
    except Exception as exc:
        raise guard(exc)
    return [
        {
            "symbol": p.symbol,
            "base": p.base,
            "quote": p.quote,
            "base_step": dec(p.base_step),
            "quote_step": dec(p.quote_step),
            "min_order_size": dec(p.min_order_size),
            "min_order_size_quote": dec(p.min_order_size_quote),
            "status": p.status,
        }
        for p in sorted(pairs.values(), key=lambda p: p.symbol)
    ]


@router.get("/paper/balances")
async def paper_balances(request: Request) -> dict[str, Any]:
    return {"balances": {k: dec(v) for k, v in (await engine(request).paper.get_balance()).items()}}


class PaperReset(BaseModel):
    starting_balance: Decimal = Field(gt=0)


@router.post("/paper/reset")
async def paper_reset(body: PaperReset, request: Request) -> dict[str, Any]:
    e = engine(request)
    if e.running:
        raise HTTPException(400, "Stop trading before resetting the paper account")
    if (await e.trades(status="OPEN", mode="PAPER")):
        raise HTTPException(400, "Close open paper positions before resetting")
    await e.paper.reset(body.starting_balance, e.config.symbol.split("-")[1])
    await e.broadcast_balances()
    return await paper_balances(request)


# ---------------------------------------------------------------- market


@router.get("/market/candles")
async def market_candles(
    request: Request,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """Chart payload for the active stream; another symbol/timeframe returns raw candles only."""
    e = engine(request)
    m = e.market
    if (symbol in (None, m.symbol)) and (timeframe in (None, m.timeframe)):
        return m.chart_payload()
    try:
        raw = await m.client.get_public_candles(symbol or m.symbol, interval_minutes(timeframe or m.timeframe))
    except Exception as exc:
        raise guard(exc)
    return {"symbol": symbol or m.symbol, "timeframe": timeframe or m.timeframe, "candles": [candle_dict(parse_candle(r)) for r in raw], "points": [], "signals": []}


@router.get("/market/ticker")
async def market_ticker(request: Request) -> dict[str, Any] | None:
    return engine(request).market.ticker


@router.get("/market/timeframes")
async def market_timeframes() -> list[str]:
    return list(TIMEFRAMES)


# ---------------------------------------------------------------- indicators


@router.get("/indicators")
async def indicators(request: Request) -> dict[str, Any]:
    cfg = engine(request).config
    return {
        "available": [cls.get_metadata() for cls in INDICATORS.values()],
        "active": {"indicator": cfg.indicator, "parameters": cfg.parameters},
    }


class ConfigureBody(BaseModel):
    indicator: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    parameters: dict[str, Any] | None = None
    position_size_type: Literal["fixed", "percent"] | None = None
    position_size_value: Decimal | None = None


@router.post("/indicators/configure")
async def configure(body: ConfigureBody, request: Request) -> dict[str, Any]:
    e = engine(request)
    try:
        if body.indicator:
            get_indicator(body.indicator)
        await e.configure(**body.model_dump())
    except Exception as exc:
        raise guard(exc)
    return e.status()


@router.get("/indicators/signals")
async def signals(request: Request, limit: int = Query(200, le=1000)) -> dict[str, Any]:
    e = engine(request)
    return {"chart": [s.to_dict() for s in e.market.signals], "recorded": await e.signals(limit)}


# ---------------------------------------------------------------- trading controls


@router.get("/trading/status")
async def trading_status(request: Request) -> dict[str, Any]:
    return engine(request).status()


@router.post("/trading/start")
async def trading_start(request: Request) -> dict[str, Any]:
    e = engine(request)
    try:
        await e.start()
    except Exception as exc:
        raise guard(exc)
    return e.status()


@router.post("/trading/stop")
async def trading_stop(request: Request) -> dict[str, Any]:
    e = engine(request)
    await e.stop()
    return e.status()


@router.post("/trading/pause")
async def trading_pause(request: Request) -> dict[str, Any]:
    e = engine(request)
    await e.pause()
    return e.status()


@router.post("/trading/resume")
async def trading_resume(request: Request) -> dict[str, Any]:
    e = engine(request)
    await e.resume()
    return e.status()


class ModeBody(BaseModel):
    mode: ExecutionMode
    confirmation: str | None = None


@router.post("/trading/mode")
async def trading_mode(body: ModeBody, request: Request) -> dict[str, Any]:
    e = engine(request)
    try:
        await e.set_mode(body.mode, body.confirmation)
    except Exception as exc:
        raise guard(exc)
    return e.status()


@router.get("/trading/orders")
async def trading_orders(request: Request, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    return await engine(request).orders(limit)


@router.get("/trading/balances")
async def trading_balances(request: Request) -> dict[str, Any]:
    return await engine(request).balances()


# ---------------------------------------------------------------- trades / performance


@router.get("/trades")
async def trades(request: Request, mode: ExecutionMode | None = None, limit: int = Query(200, le=1000)) -> list[dict]:
    return await engine(request).trades(mode=mode.value if mode else None, limit=limit)


@router.get("/trades/open")
async def trades_open(request: Request, mode: ExecutionMode | None = None) -> list[dict]:
    return await engine(request).trades(status="OPEN", mode=mode.value if mode else None)


@router.get("/trades/history")
async def trades_history(request: Request, mode: ExecutionMode | None = None, limit: int = Query(200, le=1000)) -> list[dict]:
    return await engine(request).trades(status="CLOSED", mode=mode.value if mode else None, limit=limit)


@router.post("/trades/{trade_id}/close")
async def close_trade(trade_id: int, request: Request) -> dict[str, Any]:
    try:
        return await engine(request).close_trade(trade_id)
    except Exception as exc:
        raise guard(exc)


@router.get("/performance")
async def performance(request: Request, mode: ExecutionMode | None = None) -> dict[str, Any]:
    return await engine(request).performance(mode.value if mode else None)


# ---------------------------------------------------------------- risk / emergency


class RiskBody(BaseModel):
    max_position_size: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_open_positions: int | None = None
    min_balance: Decimal | None = None


@router.post("/risk/configure")
async def risk_configure(body: RiskBody, request: Request) -> dict[str, Any]:
    e = engine(request)
    try:
        await e.configure_risk(**body.model_dump(exclude_unset=True))
    except Exception as exc:
        raise guard(exc)
    return e.status()


@router.post("/emergency-stop")
async def emergency_stop(request: Request) -> dict[str, Any]:
    e = engine(request)
    await e.trigger_emergency_stop()
    return e.status()


@router.post("/emergency-stop/reset")
async def emergency_reset(request: Request) -> dict[str, Any]:
    e = engine(request)
    await e.reset_emergency_stop()
    return e.status()


# ---------------------------------------------------------------- websocket


@ws_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    e: TradingEngine = ws.app.state.engine
    b = e.broadcaster
    await b.connect(ws)
    try:
        await b.send(ws, "trading_status", e.status())
        if e.market.ticker:
            await b.send(ws, "market_update", e.market.ticker)
        await b.send(ws, "balance_update", await e.balances())
        while True:
            await ws.receive_text()  # keepalive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        b.disconnect(ws)
