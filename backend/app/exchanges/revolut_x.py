"""Revolut X Crypto Exchange REST adapter.

Implemented against the official API reference
(https://developer.revolut.com/docs/api/revolut-x-crypto-exchange) and the official
TypeScript client in vendor/revolut-x-api (api/src/auth/signer.ts, api/src/http/request.ts).

Signing: message = timestamp + METHOD + path (from /api) + query (no '?') + minified JSON body,
signed with Ed25519 and base64-encoded.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

API_PREFIX = "/api"
SUPPORTED_INTERVALS = {1, 5, 15, 30, 60, 240, 1440, 2880, 5760, 10080, 20160, 40320}
PUBLIC_CANDLE_LIMIT = 1000


# --------------------------------------------------------------------------- errors


class RevolutXError(Exception):
    def __init__(self, message: str, status: int | None = None, error_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_id = error_id


class ConfigurationError(RevolutXError):
    pass


class AuthenticationError(RevolutXError):
    pass


class ForbiddenError(RevolutXError):
    pass


class BadRequestError(RevolutXError):
    pass


class DuplicateClientOrderIdError(BadRequestError):
    pass


class NotFoundError(RevolutXError):
    pass


class ConflictError(RevolutXError):
    pass


class RateLimitError(RevolutXError):
    def __init__(self, message: str, retry_after_ms: float | None = None):
        super().__init__(message, 429)
        self.retry_after_ms = retry_after_ms


class ServerError(RevolutXError):
    pass


class NetworkError(RevolutXError):
    """Transport-level failure. ``may_have_reached_server`` is True when a state-changing
    request could have been processed (e.g. read timeout) and must be reconciled."""

    def __init__(self, message: str, may_have_reached_server: bool):
        super().__init__(message)
        self.may_have_reached_server = may_have_reached_server


# --------------------------------------------------------------------------- signing


def load_private_key(path: str | os.PathLike, enforce_permissions: bool = True) -> Ed25519PrivateKey:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigurationError(f"Private key file not found: {p}")
    if enforce_permissions and os.name == "posix":
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise ConfigurationError(
                f"Private key file {p} is accessible by other users (mode {oct(mode)}). Run: chmod 600 {p}"
            )
    data = p.read_bytes()
    if b"ENCRYPTED" in data:
        raise ConfigurationError("Passphrase-protected private keys are not supported")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as exc:  # noqa: BLE001 - never echo key material
        raise ConfigurationError("Private key file is not a valid PEM (PKCS#8) private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ConfigurationError("Private key is not an Ed25519 key")
    return key


def build_query_string(params: dict[str, Any] | None) -> str:
    """Same canonicalisation as the official client: drop None, sort keys, encodeURIComponent."""
    if not params:
        return ""
    items = sorted((k, str(v)) for k, v in params.items() if v is not None)
    return "&".join(f"{quote(k, safe='-_.!~*()' + chr(39))}={quote(v, safe='-_.!~*()' + chr(39))}" for k, v in items)


def minify_json(body: dict[str, Any] | None) -> str:
    if body is None:
        return ""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def sign_request(
    private_key: Ed25519PrivateKey, timestamp: str, method: str, path: str, query: str = "", body: str = ""
) -> str:
    message = f"{timestamp}{method.upper()}{path}{query}{body}".encode("utf-8")
    return base64.b64encode(private_key.sign(message)).decode("ascii")


# --------------------------------------------------------------------------- rate limiting


class MinIntervalLimiter:
    """Spaces calls to an endpoint at least ``interval`` seconds apart."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval

    def penalise(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class PairInfo:
    symbol: str  # dash format, e.g. BTC-GBP
    base: str
    quote: str
    base_step: Decimal
    quote_step: Decimal
    min_order_size: Decimal
    max_order_size: Decimal
    min_order_size_quote: Decimal
    max_order_size_quote: Decimal | None
    status: str

    @classmethod
    def from_api(cls, key: str, raw: dict) -> "PairInfo":
        def d(name: str, default: str | None = "0") -> Decimal | None:
            v = raw.get(name, default)
            return None if v in (None, "") else Decimal(str(v))

        return cls(
            symbol=to_dash(key),
            base=raw["base"],
            quote=raw["quote"],
            base_step=d("base_step"),
            quote_step=d("quote_step"),
            min_order_size=d("min_order_size"),
            max_order_size=d("max_order_size"),
            min_order_size_quote=d("min_order_size_quote"),
            max_order_size_quote=d("max_order_size_quote", None),
            status=raw.get("status", "active"),
        )


def to_dash(symbol: str) -> str:
    return symbol.replace("/", "-").upper()


def to_slash(symbol: str) -> str:
    return symbol.replace("-", "/").upper()


# --------------------------------------------------------------------------- client


class RevolutXClient:
    def __init__(
        self,
        base_url: str = "https://revx.revolut.com",
        api_key: str | None = None,
        private_key: Ed25519PrivateKey | None = None,
        region: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        public_min_interval: float = 1.05,
        private_min_interval: float = 0.065,  # 1000 requests / minute
        max_get_retries: int = 2,
    ):
        base_url = base_url.rstrip("/")
        if base_url.endswith("/api") or base_url.endswith("/api/1.0"):
            base_url = base_url.rsplit("/api", 1)[0]
        if not base_url.startswith("https://") and not base_url.startswith("http://localhost") and transport is None:
            raise ConfigurationError("REVOLUT_X_BASE_URL must use https://")
        self.base_url = base_url
        self.api_key = api_key or None
        self._private_key = private_key
        self.region = region
        self.max_get_retries = max_get_retries
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._public_interval = public_min_interval
        self._public_limiters: dict[str, MinIntervalLimiter] = {}
        self._private_limiter = MinIntervalLimiter(private_min_interval)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self._private_key)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core request

    def _limiter_for(self, path: str, authenticated: bool) -> MinIntervalLimiter:
        if authenticated:
            return self._private_limiter
        key = path.split("/public/")[1].split("/")[0] if "/public/" in path else path
        if key not in self._public_limiters:
            self._public_limiters[key] = MinIntervalLimiter(self._public_interval)
        return self._public_limiters[key]

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
        version: str = "1.0",
    ) -> Any:
        method = method.upper()
        full_path = f"{API_PREFIX}/{version}{path}"
        query = build_query_string(params)
        body_str = minify_json(body)
        url = full_path + (f"?{query}" if query else "")
        limiter = self._limiter_for(full_path, authenticated)
        retries = self.max_get_retries if method == "GET" else 0

        attempt = 0
        while True:
            await limiter.wait()
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if authenticated:
                if not self.has_credentials:
                    raise ConfigurationError("Revolut X API key and private key are not configured")
                ts = str(int(time.time() * 1000))
                headers["X-Revx-API-Key"] = self.api_key  # type: ignore[assignment]
                headers["X-Revx-Timestamp"] = ts
                headers["X-Revx-Signature"] = sign_request(self._private_key, ts, method, full_path, query, body_str)  # type: ignore[arg-type]
            try:
                response = await self._http.request(
                    method, url, headers=headers, content=body_str.encode("utf-8") if body_str else None
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                err: RevolutXError = NetworkError(f"Could not reach Revolut X: {type(exc).__name__}", False)
            except httpx.TimeoutException as exc:
                err = NetworkError(f"No reply from Revolut X: {type(exc).__name__}", True)
            except httpx.TransportError as exc:
                err = NetworkError(f"Transport error talking to Revolut X: {type(exc).__name__}", True)
            else:
                if response.is_success:
                    if response.status_code == 204 or not response.content:
                        return {}
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise NetworkError("Revolut X returned an unreadable response", True) from exc
                err = self._error_from_response(response)
                if isinstance(err, RateLimitError):
                    limiter.penalise((err.retry_after_ms or 1000) / 1000)

            retryable = isinstance(err, (ServerError, RateLimitError)) or (
                isinstance(err, NetworkError) and method == "GET"
            )
            if retryable and attempt < retries:
                attempt += 1
                delay = 0.5 * 2**attempt
                if isinstance(err, RateLimitError) and err.retry_after_ms:
                    delay = min(err.retry_after_ms / 1000, 10.0)
                logger.warning("Revolut X %s %s failed (%s); retry %d in %.1fs", method, full_path, err.message, attempt, delay)
                await asyncio.sleep(delay)
                continue
            raise err

    @staticmethod
    def _error_from_response(response: httpx.Response) -> RevolutXError:
        message = f"HTTP {response.status_code}"
        error_id = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message") or message
                error_id = payload.get("error_id")
        except ValueError:
            pass
        status = response.status_code
        if status == 401:
            return AuthenticationError(message, status, error_id)
        if status == 403:
            return ForbiddenError(message, status, error_id)
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_ms = float(retry_after) if retry_after else None
            except ValueError:
                retry_ms = None
            return RateLimitError(message, retry_ms)
        if status == 400:
            if "already been placed" in message.lower():
                return DuplicateClientOrderIdError(message, status, error_id)
            return BadRequestError(message, status, error_id)
        if status == 404:
            return NotFoundError(message, status, error_id)
        if status == 409:
            return ConflictError(message, status, error_id)
        if status >= 500:
            return ServerError(message, status, error_id)
        return RevolutXError(message, status, error_id)

    def _region_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        out = dict(params or {})
        if self.region:
            out["region"] = self.region
        return out

    # ------------------------------------------------------------------ public market data

    async def get_public_tickers(self, symbols: list[str] | None = None) -> list[dict]:
        params = self._region_params({"symbols": ",".join(symbols) if symbols else None})
        data = await self.request("GET", "/public/tickers", params, authenticated=False)
        rows = data.get("data", [])
        if self.region:
            rows = [r for r in rows if r.get("region", self.region) == self.region]
        return rows

    async def get_public_pairs(self) -> dict[str, PairInfo]:
        data = await self.request("GET", "/public/configuration/pairs", self._region_params(), authenticated=False)
        data = data.get("data", data) if isinstance(data, dict) and "data" in data else data
        return {to_dash(k): PairInfo.from_api(k, v) for k, v in data.items()}

    async def get_public_currencies(self) -> dict[str, dict]:
        data = await self.request("GET", "/public/configuration/currencies", self._region_params(), authenticated=False)
        return data.get("data", data) if isinstance(data, dict) and "data" in data else data

    async def get_public_candles(
        self, symbol: str, interval: int, since: int | None = None, until: int | None = None
    ) -> list[dict]:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported candle interval: {interval}")
        params = self._region_params({"interval": interval, "since": since, "until": until})
        data = await self.request("GET", f"/public/candles/{to_dash(symbol)}", params, authenticated=False)
        return data.get("data", [])

    async def get_public_order_book(self, symbol: str, limit: int = 20) -> dict:
        params = self._region_params({"limit": limit})
        data = await self.request("GET", f"/public/order-book/{to_dash(symbol)}", params, authenticated=False, version="2.0")
        return data.get("data", {})

    # ------------------------------------------------------------------ authenticated: account & config

    async def get_balances(self) -> list[dict]:
        data = await self.request("GET", "/balances")
        return data if isinstance(data, list) else data.get("data", [])

    async def get_currencies(self) -> dict[str, dict]:
        return await self.request("GET", "/configuration/currencies")

    async def get_pairs(self) -> dict[str, PairInfo]:
        data = await self.request("GET", "/configuration/pairs")
        return {to_dash(k): PairInfo.from_api(k, v) for k, v in data.items()}

    async def get_fee_tiers(self) -> list[dict]:
        data = await self.request("GET", "/configuration/fees/tiers")
        return data.get("data", [])

    async def get_tickers(self, symbols: list[str] | None = None) -> list[dict]:
        data = await self.request("GET", "/tickers", {"symbols": ",".join(symbols) if symbols else None})
        return data.get("data", [])

    async def get_order_book(self, symbol: str, limit: int = 20) -> dict:
        data = await self.request("GET", f"/order-book/{to_dash(symbol)}", {"limit": limit})
        return data.get("data", {})

    # ------------------------------------------------------------------ orders

    async def get_active_orders(self, symbols: list[str] | None = None, limit: int = 300) -> list[dict]:
        return await self._paginate(
            "/orders/active", {"symbols": ",".join(symbols) if symbols else None, "limit": limit}
        )

    async def get_historical_orders(
        self,
        symbols: list[str] | None = None,
        start_date: int | None = None,
        end_date: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        params = {
            "symbols": ",".join(symbols) if symbols else None,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }
        return await self._paginate("/orders/historical", params)

    async def _paginate(self, path: str, params: dict[str, Any], max_pages: int = 20) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            data = await self.request("GET", path, {**params, "cursor": cursor})
            rows.extend(data.get("data", []))
            cursor = (data.get("metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return rows

    async def get_order(self, venue_order_id: str) -> dict:
        data = await self.request("GET", f"/orders/{venue_order_id}")
        return data.get("data", data)

    async def get_order_fills(self, venue_order_id: str) -> list[dict]:
        data = await self.request("GET", f"/orders/fills/{venue_order_id}")
        return data.get("data", [])

    async def get_private_trades(self, symbol: str, start_date: int | None = None, end_date: int | None = None) -> list[dict]:
        return await self._paginate(
            f"/trades/private/{to_dash(symbol)}", {"start_date": start_date, "end_date": end_date, "limit": 100}
        )

    @staticmethod
    def build_order_request(
        symbol: str,
        side: str,
        order_type: str,
        client_order_id: str | None = None,
        base_size: Decimal | None = None,
        quote_size: Decimal | None = None,
        price: Decimal | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]:
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if (base_size is None) == (quote_size is None):
            raise ValueError("Exactly one of base_size or quote_size must be provided")
        size = base_size if base_size is not None else quote_size
        if size is None or size <= 0:
            raise ValueError("Order size must be positive")
        sizing = {"base_size": _dec_str(base_size)} if base_size is not None else {"quote_size": _dec_str(quote_size)}
        if order_type == "market":
            config: dict[str, Any] = {"market": sizing}
        elif order_type == "limit":
            if price is None or price <= 0:
                raise ValueError("Limit orders require a positive price")
            limit = {**sizing, "price": _dec_str(price)}
            if post_only:
                limit["execution_instructions"] = ["post_only"]
            config = {"limit": limit}
        else:
            raise ValueError(f"Unsupported order type: {order_type}")
        return {
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "symbol": to_dash(symbol),
            "side": side,
            "order_configuration": config,
        }

    async def place_order(self, order_request: dict[str, Any]) -> dict:
        """POST /api/1.0/orders. Never retried automatically: a timeout raises NetworkError
        with may_have_reached_server=True and the caller must reconcile via client_order_id."""
        data = await self.request("POST", "/orders", body=order_request)
        return data.get("data", data)

    async def place_market_order(self, symbol: str, side: str, *, base_size=None, quote_size=None, client_order_id=None) -> dict:
        return await self.place_order(
            self.build_order_request(symbol, side, "market", client_order_id, base_size, quote_size)
        )

    async def place_limit_order(
        self, symbol: str, side: str, price: Decimal, *, base_size=None, quote_size=None, client_order_id=None, post_only=False
    ) -> dict:
        return await self.place_order(
            self.build_order_request(symbol, side, "limit", client_order_id, base_size, quote_size, price, post_only)
        )

    async def cancel_order(self, venue_order_id: str) -> None:
        await self.request("DELETE", f"/orders/{venue_order_id}")

    async def find_order_by_client_id(self, client_order_id: str, symbol: str | None = None) -> dict | None:
        """Look for an order we may have submitted (used to reconcile uncertain submissions)."""
        symbols = [to_dash(symbol)] if symbol else None
        for order in await self.get_active_orders(symbols):
            if order.get("client_order_id") == client_order_id:
                return order
        now = int(time.time() * 1000)
        for order in await self.get_historical_orders(symbols, start_date=now - 7 * 24 * 3600 * 1000 + 60_000, end_date=now):
            if order.get("client_order_id") == client_order_id:
                return order
        return None

    # ------------------------------------------------------------------ connection test

    async def test_connection(self) -> dict[str, Any]:
        """Verifies authentication, balance retrieval and pair discovery. Raises on failure."""
        balances = await self.get_balances()
        pairs = await self.get_pairs()
        return {"balances": len(balances), "pairs": len(pairs)}


def _dec_str(value: Decimal | None) -> str:
    assert value is not None
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
