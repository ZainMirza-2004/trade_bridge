import { useCallback, useEffect, useRef, useState } from "react";
import { api, connectSocket, fmt, pnlClass } from "./api";
import Chart from "./components/Chart";
import ControlPanel from "./components/ControlPanel";
import TradesPanel from "./components/TradesPanel";
import type {
  Balances, CandleUpdate, ChartPayload, IndicatorMeta, Order, Pair, Performance, SignalRecord, Ticker, Trade, TradingStatus,
} from "./types";

const TF_MS: Record<string, number> = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "30m": 1800e3, "1h": 3600e3, "4h": 14400e3, "1d": 86400e3 };

function DataBadge({ status, wsUp, lastTick }: { status: TradingStatus; wsUp: boolean; lastTick: number | null }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const age = lastTick ? Math.max(0, Math.round((now - lastTick) / 1000)) : null;
  let state = status.market_data_status;
  if (!wsUp) state = "DISCONNECTED";
  else if (state === "LIVE" && age !== null && age > status.stale_data_seconds) state = "STALE";
  const cls = state === "LIVE" ? "bg-emerald-900/60 text-emerald-300" : state === "STALE" ? "bg-amber-900/60 text-amber-300" : "bg-rose-900/70 text-rose-200";
  const title = [
    `Revolut X has no streaming market feed; prices are polled over REST every ${status.poll_interval_seconds}s.`,
    `Data is treated as stale after ${status.stale_data_seconds}s without a successful poll.`,
    !wsUp ? "Browser is not connected to the backend." : "",
    status.market_data_error ? `Last error: ${status.market_data_error}` : "",
  ].filter(Boolean).join("\n");
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-semibold ${cls}`} title={title} data-testid="data-status">
      Data {state}{age !== null && wsUp ? ` · ${age}s ago` : ""}
    </span>
  );
}

function Stat({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className="flex flex-col">
      <span className="label">{label}</span>
      <span className={`num ${className}`}>{value}</span>
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState<TradingStatus | null>(null);
  const [payload, setPayload] = useState<ChartPayload | null>(null);
  const [live, setLive] = useState<CandleUpdate | null>(null);
  const [ticker, setTicker] = useState<Ticker | null>(null);
  const [indicators, setIndicators] = useState<IndicatorMeta[]>([]);
  const [pairs, setPairs] = useState<Pair[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [history, setHistory] = useState<Trade[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [signals, setSignals] = useState<SignalRecord[]>([]);
  const [perf, setPerf] = useState<Performance | null>(null);
  const [balances, setBalances] = useState<Balances | null>(null);
  const [wsUp, setWsUp] = useState(false);
  const [lastTick, setLastTick] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const loadChart = useCallback(() => api.get<ChartPayload>("/market/candles").then(setPayload).catch((e) => setError(e.message)), []);

  const loadActivity = useCallback(async () => {
    try {
      const [o, h, ord, sig, p, b] = await Promise.all([
        api.get<Trade[]>("/trades/open"),
        api.get<Trade[]>("/trades/history"),
        api.get<Order[]>("/trading/orders"),
        api.get<{ recorded: SignalRecord[] }>("/indicators/signals?limit=200"),
        api.get<Performance>("/performance"),
        api.get<Balances>("/trading/balances"),
      ]);
      setOpenTrades(o); setHistory(h); setOrders(ord); setSignals(sig.recorded); setPerf(p); setBalances(b);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const scheduleActivity = useCallback(() => {
    clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(loadActivity, 250);
  }, [loadActivity]);

  useEffect(() => {
    api.get<TradingStatus>("/trading/status").then(setStatus).catch((e) => setError(e.message));
    api.get<{ available: IndicatorMeta[] }>("/indicators").then((d) => setIndicators(d.available)).catch(() => undefined);
    api.get<Pair[]>("/exchange/pairs").then(setPairs).catch((e) => setError(e.message));
    api.get<Ticker | null>("/market/ticker").then((t) => { setTicker(t); if (t) setLastTick(Date.now()); }).catch(() => undefined);
    loadChart();
    loadActivity();
    return connectSocket((type, data) => {
      switch (type) {
        case "trading_status": setStatus(data as TradingStatus); break;
        case "market_update": setTicker(data as Ticker); setLastTick(Date.now()); break;
        case "candle_update": setLive(data as CandleUpdate); break;
        case "balance_update": setBalances(data as Balances); break;
        case "indicator_signal": case "order_update": case "trade_opened": case "trade_closed": case "trade_updated": scheduleActivity(); break;
      }
    }, (up) => {
      setWsUp(up);
      if (up) { loadChart(); loadActivity(); }
    });
  }, [loadChart, loadActivity, scheduleActivity]);

  // unrealised P&L moves with price; refresh open positions on ticks while any are open
  useEffect(() => {
    if (openTrades.length) scheduleActivity();
  }, [ticker?.bid]); // eslint-disable-line react-hooks/exhaustive-deps

  const onConfigured = () => { loadChart(); loadActivity(); api.get<TradingStatus>("/trading/status").then(setStatus); };

  if (!status) return <div className="p-6 text-slate-400">{error ? `Backend unavailable: ${error}` : "Connecting to backend…"}</div>;

  const cfg = status.config!;
  const quote = cfg.symbol.split("-")[1];
  const base = cfg.symbol.split("-")[0];
  const change = ticker ? Number(ticker.price_change_24h) : 0;
  const warnings = [
    status.emergency_stop && "EMERGENCY STOP ACTIVE — no new orders will be placed",
    status.blocked_reason,
    status.market_data_status === "STALE" && "Market data is stale — no new orders will be placed until it recovers",
    status.market_data_status === "DISCONNECTED" && `Market data disconnected${status.market_data_error ? `: ${status.market_data_error}` : ""}`,
    !wsUp && "Live connection to backend lost — reconnecting…",
    ...status.reconciliation_warnings,
  ].filter(Boolean) as string[];

  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center gap-6 px-4 py-2 border-b border-[#1e2533] bg-[#0e121b]">
        <div className="font-semibold text-white">{cfg.symbol.replace("-", " / ")}</div>
        <div className={`num text-lg ${change >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{ticker ? fmt(ticker.mid ?? ticker.last_price) : "—"}</div>
        <Stat label="Bid" value={fmt(ticker?.bid)} />
        <Stat label="Ask" value={fmt(ticker?.ask)} />
        <Stat label="24h" value={ticker ? `${fmt(change)} (${fmt((change / (Number(ticker.last_price) - change)) * 100)}%)` : "—"} className={change >= 0 ? "text-emerald-400" : "text-rose-400"} />
        <Stat label="Timeframe" value={cfg.timeframe} />
        <Stat label="Indicator" value={indicators.find((i) => i.name === cfg.indicator)?.label ?? cfg.indicator} />
        <div className="ml-auto flex items-center gap-4">
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${status.mode === "LIVE" ? "bg-rose-700 text-white" : "bg-sky-900 text-sky-200"}`}>{status.mode}</span>
          <span className={status.running ? (status.paused ? "text-amber-300" : "text-emerald-400") : "text-slate-400"}>
            ● {status.running ? (status.paused ? "Paused" : "Auto-trading") : "Stopped"}
          </span>
          <span className="text-xs text-slate-400" title={status.connection_error ?? ""}>
            Exchange: <span className={status.connection_status === "CONNECTED" ? "text-emerald-400" : "text-slate-300"}>{status.connection_status}</span>
          </span>
          <DataBadge status={status} wsUp={wsUp} lastTick={lastTick} />
        </div>
      </header>

      {warnings.length > 0 && (
        <div className="bg-rose-950/70 border-b border-rose-800 text-rose-200 px-4 py-1.5 text-xs">{warnings.map((w) => <div key={w}>{w}</div>)}</div>
      )}
      {error && (
        <div className="bg-amber-950/60 border-b border-amber-800 text-amber-200 px-4 py-1.5 text-xs flex justify-between">
          <span>{error}</span><button onClick={() => setError(null)}>✕</button>
        </div>
      )}

      <div className="flex-1 min-h-0 grid grid-cols-[1fr_320px] gap-3 p-3">
        <div className="flex flex-col gap-3 min-h-0">
          <div className="panel flex-[3] min-h-[320px] overflow-hidden">
            <Chart payload={payload} liveUpdate={live} trades={[...openTrades, ...history]} timeframeMs={TF_MS[cfg.timeframe] ?? 60e3} />
          </div>
          <div className="panel px-4 py-2 flex flex-wrap gap-x-8 gap-y-2">
            <Stat label={`${quote} balance`} value={fmt(balances?.balances[quote])} />
            <Stat label={`${base} balance`} value={balances?.balances[base] ? String(Number(balances.balances[base])) : "0"} />
            <Stat label="Realised P&L" value={fmt(perf?.total_realized_pnl)} className={pnlClass(perf?.total_realized_pnl)} />
            <Stat label="Unrealised P&L" value={fmt(perf?.unrealized_pnl)} className={pnlClass(perf?.unrealized_pnl)} />
            <Stat label="Today" value={fmt(perf?.realized_pnl_today)} className={pnlClass(perf?.realized_pnl_today)} />
            <Stat label="Trades" value={String(perf?.total_trades ?? 0)} />
            <Stat label="Win rate" value={perf?.win_rate ? `${fmt(perf.win_rate, 1)}%` : "—"} />
            <Stat label="Avg win" value={fmt(perf?.average_win)} className="text-emerald-400" />
            <Stat label="Avg loss" value={fmt(perf?.average_loss)} className="text-rose-400" />
            <Stat label="Profit factor" value={fmt(perf?.profit_factor)} />
            <Stat label="Max drawdown" value={fmt(perf?.max_drawdown)} />
            <Stat label="Fees" value={fmt(perf?.total_fees)} />
          </div>
          <div className="flex-[2] min-h-[200px]">
            <TradesPanel open={openTrades} history={history} orders={orders} signals={signals} onError={setError} />
          </div>
        </div>
        <aside className="overflow-y-auto min-h-0">
          <ControlPanel status={status} indicators={indicators} pairs={pairs} onError={setError} onConfigured={onConfigured} />
          {status.last_skip_reason && <div className="mt-3 text-xs text-slate-500">Last skipped signal: {status.last_skip_reason}</div>}
        </aside>
      </div>
    </div>
  );
}
