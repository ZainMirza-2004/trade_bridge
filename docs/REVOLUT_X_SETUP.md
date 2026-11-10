# Revolut X setup

PAPER mode needs nothing from this page: it uses the public market-data endpoints only. Follow these steps to read your real balances and, optionally, to trade live.

Revolut X authenticates API requests with an **API key** plus an **Ed25519 signature** created with a private key that never leaves your machine. API keys **only work from allow-listed IP addresses**. A request from any other IP fails with
`401 Unauthorized: "API key can only be used for authentication from whitelisted IP"`.

## 1. Generate an Ed25519 key pair

```bash
mkdir -p ~/.config/crypto-terminal && cd ~/.config/crypto-terminal
openssl genpkey -algorithm ed25519 -out revolut_x_private.pem
openssl pkey -in revolut_x_private.pem -pubout -out revolut_x_public.pem
chmod 600 revolut_x_private.pem
cat revolut_x_public.pem          # you paste this into Revolut X
```

- The backend refuses to load a private key that group or other users can read (any bit of `0o077` set). Fix it with `chmod 600`.
- Keep the private key outside the repository. `.gitignore` excludes `*.pem`, `*.key` and `.env`, but do not rely on that.
- Never paste the private key anywhere: not into Revolut X, the frontend, or a chat.

## 2. Find the public IP address Revolut X will see

The allow-list needs your **public egress IPv4**, which is not your LAN address (`192.168.x.x`, `10.x.x.x`).

Do it on the machine that runs the backend:

```bash
curl -4 https://api.ipify.org; echo
```

or, with the backend running:

```bash
curl -s http://127.0.0.1:8000/api/exchange/public-ip
```

The dashboard also shows it: **Revolut X connection → Show this machine's public IP**.

Notes:
- If you use a VPN, proxy or cloud host, the egress IP is the VPN/proxy/host IP. Check it with the VPN connected.
- Most home broadband IPs are **dynamic** and can change after a router restart or ISP lease renewal. When that happens, authenticated calls start failing with the 401 above, and the dashboard shows `Exchange: AUTH_FAILED` and blocks live trading. Repeat steps 2–3 with the new IP. For unattended live trading, run on a host with a static IP.
- IPv6: if your machine prefers IPv6, the request may leave over IPv6. `curl -4` shows the IPv4 address; the backend uses whatever route your OS picks. If the IPv4 address is allow-listed and you still get the 401, compare it with `curl -6 https://api64.ipify.org`, or disable IPv6 for the backend host.

## 3. Allow-list your IP address

1. Open Revolut X in a web browser (https://exchange.revolut.com) and sign in.
2. Go to **Profile → API Keys** and choose **Add / Create API key**.
3. Paste the full contents of `revolut_x_public.pem` (including the `-----BEGIN PUBLIC KEY-----` and `-----END PUBLIC KEY-----` lines).
4. Enter the IP address(es) from step 2 in the **IP allow-list / whitelisted IPs** field. Add only the addresses you will run the backend from.
5. Choose permissions:
   - **Read** (balances, orders, fills) is enough for paper trading with real balance display and for `Test connection`.
   - **Trade** is needed only for LIVE mode. Do not grant withdrawal or transfer permissions; this app never uses them.
6. Confirm in the Revolut app if asked, then copy the 64-character **API key**.

The exact labels in the Revolut X web UI may differ slightly by region or version. The required pieces are always the public key, the allow-listed IP(s) and the generated API key. To change the IP later, edit the key's allow-list, or create a new key and revoke the old one.

## 4. Configure the backend

In `.env` at the repository root:

```ini
REVOLUT_X_API_KEY=<64-character key>
REVOLUT_X_PRIVATE_KEY_PATH=/home/<you>/.config/crypto-terminal/revolut_x_private.pem
REVOLUT_X_BASE_URL=https://revx.revolut.com
```

Restart the backend. Credentials are read only by the backend; the frontend never sees them and they are never logged. Every request is signed as:

```
signature = base64(Ed25519_sign(private_key, timestamp + METHOD + path + query + minified_json_body))
headers   = X-Revx-API-Key, X-Revx-Timestamp (unix ms), X-Revx-Signature
```

Revolut X rejects timestamps that are too far off (`409 Request timestamp is in the future`), so keep the machine clock NTP-synced (`timedatectl status` on Linux should say `System clock synchronized: yes`).

## 5. Verify the connection

Check each step in order. All of them are read-only and none places orders.

1. **Credentials loaded**
   ```bash
   curl -s http://127.0.0.1:8000/api/exchange/status
   ```
   You want `"credentials_configured": true`. If it is `false`, check `.env` and the key path and permissions; the backend log shows why the key was rejected.

2. **Authenticated call from this IP**
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/exchange/test-connection
   ```
   This calls `GET /api/1.0/balances` and `GET /api/1.0/configuration/pairs`. Success returns `{"balances": N, "pairs": M, "active_pair_available": true}`. Dashboard: **Revolut X connection → Test connection**.

   | Result | Meaning / fix |
   | --- | --- |
   | `401 ... whitelisted IP` | The IP from step 2 is not on the key's allow-list (or your IP changed or you are on a VPN). Update the allow-list. |
   | `401 Unauthorized` (other message) | Wrong API key, or the private key does not match the public key registered for that API key. |
   | `403 Forbidden` | The key lacks permission for the endpoint. |
   | `409 Request timestamp is in the future` | Clock drift; sync NTP. |
   | `active_pair_available: false` | The selected pair is not tradeable on your account; pick another pair. |

3. **Real balances**
   ```bash
   curl -s http://127.0.0.1:8000/api/exchange/balances
   ```

4. **Reconciliation dry run** (read-only, does not switch mode)
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/exchange/reconcile
   ```
   This returns `{"ok": true|false, "errors": [...], "warnings": [...]}`. It is the same check that gates LIVE mode, described in [TRADING_LOGIC.md](TRADING_LOGIC.md#live-startup-reconciliation). Dashboard: **Revolut X connection → Reconcile**.

## 6. Enabling live trading

Only do this after paper trading has behaved as you expect for a while.

1. Stop automated trading.
2. Click **Enable live…** and type `ENABLE LIVE TRADING` exactly. Via the API: `POST /api/trading/mode {"mode":"LIVE","confirmation":"ENABLE LIVE TRADING"}`.
3. The backend checks the connection, then runs the full live startup reconciliation. LIVE is refused if anything is unresolved: open orders on the pair, local orders with unknown outcome, fills that disagree with local records, or an open LIVE position not covered by your balance.
4. Click **Start**. The reconciliation runs again before automation starts.

The mode always resets to PAPER when the backend restarts. Switching back is one click (**Switch to paper**), allowed whenever automation is stopped.

Start small: use a fixed quote size near the pair minimum, set **Max position** and **Max daily loss**, and watch the first live round trip in the Orders tab and in Revolut X.

## Rate limits and polling

| Data | Endpoint | Limit (Revolut X) | Our usage |
| --- | --- | --- | --- |
| Ticker | `GET /api/1.0/public/tickers` | public, ~1 req/s per endpoint | 1 request every `MARKET_POLL_INTERVAL` (3 s) |
| Candles | `GET /api/1.0/public/candles/{symbol}` | public, ~1 req/s per endpoint, ≤1000 candles | 1 small request (last few candles) every 3 s; 999 on pair/timeframe change |
| Pairs | `GET /api/1.0/public/configuration/pairs` | public | at startup and hourly |
| Authenticated | balances, orders, fills | 1000 req/min | order status polled every ~0.5 s only while an order is in flight; reconciliation every 10 s only when a local order is unresolved |

Revolut X has no public market-data WebSocket, so prices update every poll interval (3 s by default), and the UI never shows faster updates than that. Public requests go through a client-side limiter (≥1.05 s between calls to the same endpoint); authenticated requests are spaced ≥65 ms apart. HTTP 429 responses are honoured using `Retry-After`. GET requests are retried with backoff; order placement and cancellation are **never** retried automatically.
