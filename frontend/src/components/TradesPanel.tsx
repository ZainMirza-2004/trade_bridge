import { useState } from "react";
import { api, fmt, fmtQty, fmtTime, pnlClass } from "../api";
import type { Order, SignalRecord, Trade } from "../types";

interface Props {
  open: Trade[];
  history: Trade[];
  orders: Order[];
  signals: SignalRecord[];
  onError: (m: string) => void;
}

const TABS = ["Positions", "Trade history", "Signals", "Orders"] as const;

const duration = (s: number | null) => {
  if (s === null) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
};

export default function TradesPanel({ open, history, orders, signals, onError }: Props) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Positions");
  const th = "text-left font-medium text-slate-500 px-2 py-1 whitespace-nowrap";
  const td = "px-2 py-1 whitespace-nowrap num";

  const close = async (id: number) => {
    if (!window.confirm(`Close position #${id} at market?`)) return;
    try {
      await api.post(`/trades/${id}/close`);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <div className="panel flex flex-col h-full min-h-0">
      <div className="flex gap-1 border-b border-[#1e2533] px-2">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`px-3 py-2 ${tab === t ? "text-white border-b-2 border-sky-400" : "text-slate-500"}`}>
            {t}
            {t === "Positions" && open.length > 0 && <span className="ml-1 text-sky-300">({open.length})</span>}
          </button>
        ))}
      </div>
      <div className="overflow-auto flex-1">
        {tab === "Positions" && (
          <table className="w-full">
            <thead><tr>{["#", "Mode", "Pair", "Entry time", "Entry", "Qty", "Cost", "Fees", "Unrealised P&L", ""].map((h) => <th key={h} className={th}>{h}</th>)}</tr></thead>
            <tbody>
              {open.length === 0 && <tr><td className="px-2 py-3 text-slate-500" colSpan={10}>No open positions</td></tr>}
              {open.map((t) => (
                <tr key={t.id} className="border-t border-[#1a2030]">
                  <td className={td}>{t.id}</td><td className={td}>{t.mode}</td><td className={td}>{t.symbol}</td>
                  <td className={td}>{fmtTime(t.entry_ts)}</td><td className={td}>{fmt(t.entry_price)}</td><td className={td}>{fmtQty(t.open_quantity)}</td>
                  <td className={td}>{fmt(t.entry_cost)}</td><td className={td}>{fmt(t.fees, 4)}</td>
                  <td className={`${td} ${pnlClass(t.unrealized_pnl)}`}>{fmt(t.unrealized_pnl)}</td>
                  <td className={td}><button className="btn py-0" onClick={() => close(t.id)}>Close</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "Trade history" && (
          <table className="w-full">
            <thead><tr>{["#", "Pair", "Entry", "Exit", "Entry time", "Exit time", "Duration", "Qty", "Fees", "P&L", "P&L %"].map((h) => <th key={h} className={th}>{h}</th>)}</tr></thead>
            <tbody>
              {history.length === 0 && <tr><td className="px-2 py-3 text-slate-500" colSpan={11}>No completed trades</td></tr>}
              {history.map((t) => (
                <tr key={t.id} className="border-t border-[#1a2030]">
                  <td className={td}>{t.id}</td><td className={td}>{t.symbol}</td><td className={td}>{fmt(t.entry_price)}</td><td className={td}>{fmt(t.exit_price)}</td>
                  <td className={td}>{fmtTime(t.entry_ts)}</td><td className={td}>{fmtTime(t.exit_ts)}</td><td className={td}>{duration(t.duration_seconds)}</td>
                  <td className={td}>{fmtQty(t.quantity)}</td><td className={td}>{fmt(t.fees, 4)}</td>
                  <td className={`${td} ${pnlClass(t.realized_pnl)}`}>{fmt(t.realized_pnl)}</td>
                  <td className={`${td} ${pnlClass(t.realized_pnl)}`}>{fmt(t.realized_pnl_pct)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "Signals" && (
          <table className="w-full">
            <thead><tr>{["Candle", "Signal", "Price", "Indicator", "TF", "Mode", "Status", "Reason"].map((h) => <th key={h} className={th}>{h}</th>)}</tr></thead>
            <tbody>
              {signals.length === 0 && <tr><td className="px-2 py-3 text-slate-500" colSpan={8}>No live signals recorded yet (historical signals are shown on the chart)</td></tr>}
              {signals.map((s) => (
                <tr key={s.id} className="border-t border-[#1a2030]">
                  <td className={td}>{fmtTime(s.candle_ts)}</td>
                  <td className={`${td} ${s.signal_type === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>{s.signal_type}</td>
                  <td className={td}>{fmt(s.signal_price)}</td><td className={td}>{s.indicator}</td><td className={td}>{s.timeframe}</td><td className={td}>{s.mode}</td>
                  <td className={td}>{s.execution_status}</td><td className="px-2 py-1 text-slate-400">{s.reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === "Orders" && (
          <table className="w-full">
            <thead><tr>{["#", "Time", "Mode", "Side", "Pair", "Status", "Requested", "Filled", "Avg price", "Fees", "Client ID", "Reason"].map((h) => <th key={h} className={th}>{h}</th>)}</tr></thead>
            <tbody>
              {orders.length === 0 && <tr><td className="px-2 py-3 text-slate-500" colSpan={12}>No orders</td></tr>}
              {orders.map((o) => (
                <tr key={o.id} className="border-t border-[#1a2030]">
                  <td className={td}>{o.id}</td><td className={td}>{fmtTime(o.created_at)}</td><td className={td}>{o.mode}</td>
                  <td className={`${td} ${o.side === "buy" ? "text-emerald-400" : "text-rose-400"}`}>{o.side.toUpperCase()}</td>
                  <td className={td}>{o.symbol}</td><td className={td}>{o.status}</td>
                  <td className={td}>{o.requested_quote_amount ? `${fmt(o.requested_quote_amount)} quote` : fmtQty(o.requested_quantity)}</td>
                  <td className={td}>{fmtQty(o.filled_quantity)}</td><td className={td}>{fmt(o.average_fill_price)}</td>
                  <td className={td}>{fmt(o.fees, 4)} {o.fee_currency ?? ""}</td><td className={`${td} text-slate-500`}>{o.client_order_id.slice(0, 8)}</td>
                  <td className="px-2 py-1 text-slate-400">{o.reject_reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
