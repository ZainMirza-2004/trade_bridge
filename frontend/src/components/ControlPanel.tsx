import { useEffect, useState } from "react";
import { api } from "../api";
import type { IndicatorMeta, Pair, TradingStatus } from "../types";

interface Props {
  status: TradingStatus;
  indicators: IndicatorMeta[];
  pairs: Pair[];
  onError: (msg: string) => void;
  onConfigured: () => void;
}

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

export default function ControlPanel({ status, indicators, pairs, onError, onConfigured }: Props) {
  const cfg = status.config!;
  const [indicator, setIndicator] = useState(cfg.indicator);
  const [params, setParams] = useState<Record<string, number | boolean>>(cfg.parameters);
  const [sizeType, setSizeType] = useState(cfg.position_size_type);
  const [sizeValue, setSizeValue] = useState(cfg.position_size_value);
  const [risk, setRisk] = useState({
    max_position_size: status.risk.max_position_size ?? "",
    max_daily_loss: status.risk.max_daily_loss ?? "",
    max_open_positions: String(status.risk.max_open_positions),
    min_balance: status.risk.min_balance ?? "0",
  });
  const [busy, setBusy] = useState(false);
  const [publicIp, setPublicIp] = useState<string | null>(null);
  const [connMsg, setConnMsg] = useState<string | null>(null);

  useEffect(() => {
    setIndicator(cfg.indicator);
    setParams(cfg.parameters);
    setSizeType(cfg.position_size_type);
    setSizeValue(cfg.position_size_value);
  }, [cfg.indicator, cfg.parameters, cfg.position_size_type, cfg.position_size_value]);

  const meta = indicators.find((i) => i.name === indicator);
  const locked = status.running;

  const run = async (fn: () => Promise<unknown>, refresh = false) => {
    setBusy(true);
    try {
      await fn();
      if (refresh) onConfigured();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const configure = (body: Record<string, unknown>) => run(() => api.post("/indicators/configure", body), true);

  const changeIndicator = (name: string) => {
    setIndicator(name);
    const m = indicators.find((i) => i.name === name);
    const defaults = Object.fromEntries((m?.params ?? []).map((p) => [p.name, p.default]));
    setParams(defaults);
    configure({ indicator: name, parameters: defaults });
  };

  const switchMode = () => {
    if (status.mode === "LIVE") return run(() => api.post("/trading/mode", { mode: "PAPER" }));
    const phrase = window.prompt(
      `LIVE TRADING sends real orders to Revolut X with real funds.\n\nType "${status.live_confirmation_phrase}" to enable it.`,
    );
    if (phrase === null) return;
    return run(() => api.post("/trading/mode", { mode: "LIVE", confirmation: phrase }));
  };

  const testConnection = () =>
    run(async () => {
      setConnMsg(null);
      try {
        const r = await api.post<{ active_pair_available?: boolean }>("/exchange/test-connection");
        setConnMsg(`Connected. ${cfg.symbol} ${r.active_pair_available ? "is" : "is NOT"} available on your account.`);
      } catch (e) {
        setConnMsg((e as Error).message);
      }
    });

  const findIp = () =>
    run(async () => {
      const r = await api.get<{ ip: string }>("/exchange/public-ip");
      setPublicIp(r.ip);
    });

  const check = status.live_check;

  const num = (v: string) => (v.trim() === "" ? null : v.trim());

  return (
    <div className="flex flex-col gap-3">
      <section className="panel p-3 flex flex-col gap-2">
        <div className="label">Automated trading</div>
        <div className="flex gap-2">
          {status.running ? (
            <button className="btn flex-1 border-amber-600 text-amber-300" disabled={busy} onClick={() => run(() => api.post("/trading/stop"))}>
              Stop
            </button>
          ) : (
            <button className="btn flex-1 border-emerald-700 text-emerald-300" disabled={busy || status.emergency_stop} onClick={() => run(() => api.post("/trading/start"))}>
              Start
            </button>
          )}
          <button className="btn flex-1" disabled={busy} onClick={() => run(() => api.post(status.paused ? "/trading/resume" : "/trading/pause"))}>
            {status.paused ? "Resume" : "Pause"}
          </button>
        </div>
        {status.emergency_stop ? (
          <button className="btn border-slate-500" disabled={busy} onClick={() => run(() => api.post("/emergency-stop/reset"))}>
            Reset emergency stop
          </button>
        ) : (
          <button className="btn bg-rose-900/60 border-rose-700 text-rose-100 font-semibold" onClick={() => run(() => api.post("/emergency-stop"))}>
            EMERGENCY STOP
          </button>
        )}
        <div className="flex items-center justify-between">
          <span>
            Mode: <b className={status.mode === "LIVE" ? "text-rose-400" : "text-sky-300"}>{status.mode}</b>
          </span>
          <button className="btn" disabled={busy || status.running} onClick={switchMode}>
            {status.mode === "LIVE" ? "Switch to paper" : "Enable live…"}
          </button>
        </div>
        {!status.credentials_configured && <div className="text-[11px] text-slate-500">Live trading needs API credentials (see docs/REVOLUT_X_SETUP.md).</div>}
      </section>

      <section className="panel p-3 flex flex-col gap-2">
        <div className="label">Revolut X connection</div>
        <div className="text-xs">
          Status:{" "}
          <b className={status.connection_status === "CONNECTED" ? "text-emerald-400" : "text-slate-300"}>{status.connection_status}</b>
          {!status.credentials_configured && <span className="text-slate-500"> (no credentials)</span>}
        </div>
        {status.connection_error && <div className="text-[11px] text-rose-300 break-words">{status.connection_error}</div>}
        <div className="grid grid-cols-2 gap-2">
          <button className="btn" disabled={busy || !status.credentials_configured} onClick={testConnection}>Test connection</button>
          <button className="btn" disabled={busy || !status.credentials_configured} onClick={() => run(() => api.post("/exchange/reconcile"))}>Reconcile</button>
        </div>
        <button className="btn" disabled={busy} onClick={findIp}>Show this machine's public IP</button>
        {publicIp && <div className="text-xs">Allow-list this IP on your API key: <b className="num text-sky-300 select-all">{publicIp}</b></div>}
        {connMsg && <div className="text-[11px] text-slate-300 break-words">{connMsg}</div>}
        {check && (
          <div className="text-[11px] flex flex-col gap-1" data-testid="live-check">
            <div className={check.ok ? "text-emerald-400" : "text-rose-400"}>
              Reconciliation {check.ok ? "passed" : "failed"} · {new Date(check.checked_at).toLocaleTimeString()}
            </div>
            {check.errors.map((m) => <div key={m} className="text-rose-300">✕ {m}</div>)}
            {check.warnings.map((m) => <div key={m} className="text-amber-300">! {m}</div>)}
          </div>
        )}
      </section>

      <section className="panel p-3 flex flex-col gap-2">
        <div className="label">Strategy {locked && <span className="normal-case text-amber-400">(stop trading to edit)</span>}</div>
        <div className="grid grid-cols-2 gap-2">
          <label>
            <div className="label">Pair</div>
            <select className="input" disabled={locked || busy} value={cfg.symbol} onChange={(e) => configure({ symbol: e.target.value })}>
              {pairs.map((p) => (
                <option key={p.symbol} value={p.symbol}>{p.symbol}</option>
              ))}
            </select>
          </label>
          <label>
            <div className="label">Timeframe</div>
            <select className="input" disabled={locked || busy} value={cfg.timeframe} onChange={(e) => configure({ timeframe: e.target.value })}>
              {TIMEFRAMES.map((tf) => (
                <option key={tf}>{tf}</option>
              ))}
            </select>
          </label>
        </div>
        <label>
          <div className="label">Indicator</div>
          <select className="input" disabled={locked || busy} value={indicator} onChange={(e) => changeIndicator(e.target.value)}>
            {indicators.map((i) => (
              <option key={i.name} value={i.name}>{i.label}</option>
            ))}
          </select>
        </label>
        <div className="grid grid-cols-2 gap-2">
          {meta?.params.map((p) => (
            <label key={p.name} title={p.description}>
              <div className="label">{p.label}</div>
              {p.type === "bool" ? (
                <input type="checkbox" disabled={locked} checked={Boolean(params[p.name])} onChange={(e) => setParams({ ...params, [p.name]: e.target.checked })} />
              ) : (
                <input
                  className="input num"
                  type="number"
                  disabled={locked}
                  step={p.step ?? "any"}
                  min={p.min ?? undefined}
                  max={p.max ?? undefined}
                  value={String(params[p.name] ?? "")}
                  onChange={(e) => setParams({ ...params, [p.name]: Number(e.target.value) })}
                />
              )}
            </label>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label>
            <div className="label">Size type</div>
            <select className="input" disabled={locked} value={sizeType} onChange={(e) => setSizeType(e.target.value as "fixed" | "percent")}>
              <option value="fixed">Fixed quote amount</option>
              <option value="percent">% of balance</option>
            </select>
          </label>
          <label>
            <div className="label">{sizeType === "percent" ? "Percent" : "Amount"}</div>
            <input className="input num" disabled={locked} value={sizeValue} onChange={(e) => setSizeValue(e.target.value)} />
          </label>
        </div>
        <button
          className="btn"
          disabled={locked || busy}
          onClick={() => configure({ indicator, parameters: params, position_size_type: sizeType, position_size_value: sizeValue })}
        >
          Apply strategy
        </button>
      </section>

      <section className="panel p-3 flex flex-col gap-2">
        <div className="label">Risk limits</div>
        <div className="grid grid-cols-2 gap-2">
          {(
            [
              ["max_position_size", "Max position (quote)"],
              ["max_daily_loss", "Max daily loss"],
              ["max_open_positions", "Max open positions"],
              ["min_balance", "Min balance"],
            ] as const
          ).map(([k, label]) => (
            <label key={k}>
              <div className="label">{label}</div>
              <input className="input num" placeholder="none" value={risk[k]} onChange={(e) => setRisk({ ...risk, [k]: e.target.value })} />
            </label>
          ))}
        </div>
        <button
          className="btn"
          disabled={busy}
          onClick={() =>
            run(() =>
              api.post("/risk/configure", {
                max_position_size: num(risk.max_position_size),
                max_daily_loss: num(risk.max_daily_loss),
                max_open_positions: Number(risk.max_open_positions || 1),
                min_balance: num(risk.min_balance) ?? "0",
              }),
            )
          }
        >
          Save risk limits
        </button>
      </section>
    </div>
  );
}
