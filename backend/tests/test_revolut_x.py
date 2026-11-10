"""Revolut X client and live adapter tests. All HTTP is served by httpx.MockTransport."""

import base64
import json
import os
import stat
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.core.enums import OrderStatus
from app.exchanges import revolut_x as rx
from app.exchanges.revolut_x import (
    AuthenticationError,
    DuplicateClientOrderIdError,
    NetworkError,
    RateLimitError,
    RevolutXClient,
    build_query_string,
    load_private_key,
    minify_json,
    sign_request,
)
from app.trading.execution import OrderUncertainError, RevolutXExecutionAdapter, parse_exchange_order
from tests.fakes import PAIR

# Test-only key generated for these vectors; never used against any real account.
TEST_KEY_PEM = b"""-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEILUkTLGz+XYBu1Uyd4HwUoHg3+wWuj/BPa92yspGj34y
-----END PRIVATE KEY-----
"""
KEY = load_pem_private_key(TEST_KEY_PEM, password=None)

# Expected signatures produced with Node's crypto.sign(null, ...) exactly as the official
# TypeScript client does (vendor/revolut-x-api/api/src/auth/signer.ts).
NODE_VECTORS = [
    (("1700000000000", "GET", "/api/1.0/balances", "", ""),
     "MwVFSn00hAwTcytfXBk5OLJhugbRX0wpfS+cbAuxF/F4MMD3/4alke1Qli+KmpDkbq5mcSmno7N53euGVReNBA=="),
    (("1700000000123", "POST", "/api/1.0/orders", "",
      '{"client_order_id":"abc","symbol":"BTC-GBP","side":"buy","order_configuration":{"market":{"quote_size":"100"}}}'),
     "vObcr+6HlZ+By9mYRuP05n2DByU0lkc/7/1sogm2nXBQ2QAImlsMRcfE446W65j8rhRQft+4JplwdHcabepEAw=="),
    (("1700000000456", "GET", "/api/1.0/orders/active", "symbols=BTC-GBP&limit=100", ""),
     "aQ3dnBSOSQr/8Q0z925VV8peNOsFsu2e9OhN5bvN1GnolzLoUhjA/40r9Z8VCc1dUMar5i/hLhUgPoxRuweQDQ=="),
]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(rx.asyncio, "sleep", fake_sleep)
    return slept


def client_with(handler, **kw):
    return RevolutXClient(
        api_key="k" * 64, private_key=KEY, region="UK", transport=httpx.MockTransport(handler),
        public_min_interval=0, private_min_interval=0, **kw,
    )


# ------------------------------------------------------------------ signing


@pytest.mark.parametrize("args,expected", NODE_VECTORS)
def test_signature_matches_official_node_signer(args, expected):
    assert sign_request(KEY, *args) == expected


def test_minify_and_query_canonicalisation():
    body = {"a": 1, "b": {"c": "x y"}}
    assert minify_json(body) == '{"a":1,"b":{"c":"x y"}}'
    assert build_query_string({"symbols": "BTC-GBP,ETH-GBP", "limit": 10, "cursor": None}) == "limit=10&symbols=BTC-GBP%2CETH-GBP"


def test_load_private_key_enforces_permissions(tmp_path):
    p = tmp_path / "key.pem"
    p.write_bytes(TEST_KEY_PEM)
    os.chmod(p, 0o644)
    with pytest.raises(rx.ConfigurationError):
        load_private_key(p)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    assert load_private_key(p).public_key() is not None


async def test_authenticated_request_headers_are_valid_signature():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(req.headers)
        seen["path"] = req.url.path
        seen["query"] = req.url.query.decode()
        return httpx.Response(200, json=[{"currency": "GBP", "available": "12.5", "reserved": "0", "total": "12.5"}])

    c = client_with(handler)
    balances = await c.get_balances()
    assert balances[0]["currency"] == "GBP"
    msg = f"{seen['x-revx-timestamp']}GET{seen['path']}{seen['query']}".encode()
    KEY.public_key().verify(base64.b64decode(seen["x-revx-signature"]), msg)  # raises if invalid
    assert seen["x-revx-api-key"] == "k" * 64 and seen["path"] == "/api/1.0/balances"


# ------------------------------------------------------------------ public data


async def test_public_tickers_filter_region_and_no_auth_headers():
    def handler(req):
        assert "x-revx-signature" not in req.headers
        assert req.url.params["region"] == "UK"
        return httpx.Response(200, json={"data": [
            {"symbol": "BTC/GBP", "bid": "1", "ask": "2", "region": "UK"},
            {"symbol": "BTC/GBP", "bid": "3", "ask": "4", "region": "EEA"},
        ]})

    rows = await client_with(handler).get_public_tickers(["BTC/GBP"])
    assert len(rows) == 1 and rows[0]["bid"] == "1"


async def test_public_candles_and_pairs():
    def handler(req):
        if "candles" in req.url.path:
            assert req.url.path == "/api/1.0/public/candles/BTC-GBP" and req.url.params["interval"] == "5"
            return httpx.Response(200, json={"data": [{"start": 1, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "3"}]})
        return httpx.Response(200, json={"data": {"BTC/GBP": {"base": "BTC", "quote": "GBP", "base_step": "0.00000001", "quote_step": "0.01",
                                                          "min_order_size": "0.00001", "max_order_size": "100", "min_order_size_quote": "1", "status": "active"}}})

    c = client_with(handler)
    assert (await c.get_public_candles("BTC/GBP", 5))[0]["close"] == "1.5"
    pairs = await c.get_public_pairs()
    assert pairs["BTC-GBP"].base_step == Decimal("0.00000001") and pairs["BTC-GBP"].quote == "GBP"
    with pytest.raises(ValueError):
        await c.get_public_candles("BTC-GBP", 7)


# ------------------------------------------------------------------ orders


def test_order_request_construction():
    r = RevolutXClient.build_order_request("btc/gbp", "BUY", "market", "id-1", quote_size=Decimal("100.50"))
    assert r == {"client_order_id": "id-1", "symbol": "BTC-GBP", "side": "buy", "order_configuration": {"market": {"quote_size": "100.5"}}}
    s = RevolutXClient.build_order_request("BTC-GBP", "sell", "market", "id-2", base_size=Decimal("0.00100000"))
    assert s["order_configuration"] == {"market": {"base_size": "0.001"}}
    with pytest.raises(ValueError):
        RevolutXClient.build_order_request("BTC-GBP", "buy", "market", base_size=Decimal(1), quote_size=Decimal(1))
    with pytest.raises(ValueError):
        RevolutXClient.build_order_request("BTC-GBP", "buy", "limit", quote_size=Decimal(1))


async def test_order_post_is_never_retried_on_timeout():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("slow", request=req)

    c = client_with(handler)
    with pytest.raises(NetworkError) as ei:
        await c.place_order(c.build_order_request("BTC-GBP", "buy", "market", "x", quote_size=Decimal(10)))
    assert ei.value.may_have_reached_server and len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["client_order_id"] == "x"


async def test_connect_error_is_marked_not_sent():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(NetworkError) as ei:
        await client_with(handler).place_order(RevolutXClient.build_order_request("BTC-GBP", "buy", "market", "y", quote_size=Decimal(10)))
    assert not ei.value.may_have_reached_server


async def test_get_retries_on_server_error_and_rate_limit(no_sleep):
    responses = [httpx.Response(503, json={"message": "down"}), httpx.Response(429, headers={"Retry-After": "1500"}, json={"message": "slow down"}),
                 httpx.Response(200, json=[])]

    c = client_with(lambda req: responses.pop(0))
    assert await c.get_balances() == []
    assert 1.5 in no_sleep


async def test_rate_limit_error_surfaces_after_retries():
    c = client_with(lambda req: httpx.Response(429, json={"message": "Too many"}), max_get_retries=0)
    with pytest.raises(RateLimitError):
        await c.get_balances()


async def test_error_mapping():
    c = client_with(lambda req: httpx.Response(401, json={"message": "bad key", "error_id": "e1"}))
    with pytest.raises(AuthenticationError):
        await c.get_balances()
    c = client_with(lambda req: httpx.Response(400, json={"message": "Order with client_order_id has already been placed"}))
    with pytest.raises(DuplicateClientOrderIdError):
        await c.place_order(c.build_order_request("BTC-GBP", "buy", "market", "z", quote_size=Decimal(10)))


async def test_unconfigured_client_refuses_private_calls():
    c = RevolutXClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(rx.ConfigurationError):
        await c.get_balances()


# ------------------------------------------------------------------ live execution adapter


async def pair_provider(symbol):
    return PAIR


def order_detail(status, filled="0", avg=None, fee=None, fee_ccy="GBP"):
    d = {"id": "venue-1", "client_order_id": "c1", "symbol": "BTC/GBP", "side": "buy", "type": "market", "status": status,
         "quantity": "0.002", "filled_quantity": filled}
    if avg:
        d["average_fill_price"] = avg
    if fee:
        d["total_fee"], d["fee_currency"] = fee, fee_ccy
    return {"data": d}


async def test_adapter_buy_waits_for_terminal_state_and_parses_fills():
    states = [order_detail("new"), order_detail("partially_filled", "0.001", "50000"), order_detail("filled", "0.002", "50000", "0.09")]
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"data": {"venue_order_id": "venue-1", "client_order_id": "c1", "state": "new"}})
        return httpx.Response(200, json=states.pop(0))

    adapter = RevolutXExecutionAdapter(client_with(handler), pair_provider, poll_interval=0, status_timeout=5)
    result = await adapter.buy("BTC-GBP", Decimal("100.009"), "c1")
    assert posted[0]["order_configuration"] == {"market": {"quote_size": "100"}}  # rounded down to quote_step
    assert result.status == OrderStatus.FILLED and result.filled_quantity == Decimal("0.002")
    assert result.average_fill_price == Decimal(50000) and result.fee_quote == Decimal("0.09")


async def test_adapter_timeout_raises_uncertain():
    def handler(req):
        raise httpx.ReadTimeout("t", request=req)

    adapter = RevolutXExecutionAdapter(client_with(handler), pair_provider)
    with pytest.raises(OrderUncertainError):
        await adapter.sell("BTC-GBP", Decimal("0.01"), "c2")


async def test_adapter_rejected_on_bad_request():
    adapter = RevolutXExecutionAdapter(client_with(lambda r: httpx.Response(400, json={"message": "Insufficient balance"})), pair_provider)
    r = await adapter.buy("BTC-GBP", Decimal(50), "c3")
    assert r.status == OrderStatus.REJECTED and r.reject_reason == "Insufficient balance"


async def test_adapter_server_error_on_post_is_uncertain():
    adapter = RevolutXExecutionAdapter(client_with(lambda r: httpx.Response(502, json={"message": "bad gateway"})), pair_provider)
    with pytest.raises(OrderUncertainError):
        await adapter.buy("BTC-GBP", Decimal(50), "c4")


def test_parse_order_with_base_fee():
    raw = order_detail("filled", "0.002", "50000", "0.000002", "BTC")["data"]
    r = parse_exchange_order(raw, PAIR)
    assert r.net_base_quantity == Decimal("0.001998") and r.fee_quote == Decimal("0.1")
