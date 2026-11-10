import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { CandleData, CandleUpdate, ChartPayload, IndicatorPointData, Trade } from "../types";

const UP = "#22c55e";
const DOWN = "#ef4444";
const t = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;

function overlayPoint(indicator: string | undefined, p: IndicatorPointData) {
  const key = indicator === "supertrend" ? "supertrend" : "trailing_stop";
  const value = p.values[key];
  if (value === null || value === undefined) return null;
  const bullish = indicator === "supertrend" ? p.values.direction === -1 : (p.values.close ?? 0) > value;
  return { time: t(p.time), value, color: bullish ? UP : DOWN };
}

const candleBar = (c: CandleData) => ({ time: t(c.time), open: c.open, high: c.high, low: c.low, close: c.close });
const volumeBar = (c: CandleData) => ({ time: t(c.time), value: c.volume, color: c.close >= c.open ? "#22c55e40" : "#ef444440" });

interface Props {
  payload: ChartPayload | null;
  liveUpdate: CandleUpdate | null;
  trades: Trade[];
  timeframeMs: number;
}

export default function Chart({ payload, liveUpdate, trades, timeframeMs }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const overlay = useRef<ISeriesApi<"Line"> | null>(null);
  const markersApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const signalMarkers = useRef<SeriesMarker<Time>[]>([]);
  const tradeMarkers = useRef<SeriesMarker<Time>[]>([]);
  const firstCandle = useRef<number>(0);

  const renderMarkers = () => {
    const all = [...signalMarkers.current, ...tradeMarkers.current].sort((a, b) => (a.time as number) - (b.time as number));
    markersApi.current?.setMarkers(all);
  };

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b0e14" }, textColor: "#8b95a7", attributionLogo: true },
      grid: { vertLines: { color: "#151b27" }, horzLines: { color: "#151b27" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1e2533" },
      timeScale: { borderColor: "#1e2533", timeVisible: true, secondsVisible: false },
    });
    candles.current = c.addSeries(CandlestickSeries, { upColor: UP, downColor: DOWN, wickUpColor: UP, wickDownColor: DOWN, borderVisible: false });
    volume.current = c.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    overlay.current = c.addSeries(LineSeries, { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    markersApi.current = createSeriesMarkers(candles.current, []);
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    if (!payload || !candles.current) return;
    const ind = payload.indicator?.name;
    candles.current.setData(payload.candles.map(candleBar));
    volume.current!.setData(payload.candles.map(volumeBar));
    overlay.current!.setData(payload.points.map((p) => overlayPoint(ind, p)).filter((x) => x !== null));
    firstCandle.current = payload.candles[0]?.time ?? 0;
    signalMarkers.current = payload.signals.map((s) => ({
      time: t(s.candle_ts),
      position: s.signal_type === "BUY" ? "belowBar" : "aboveBar",
      shape: s.signal_type === "BUY" ? "arrowUp" : "arrowDown",
      color: s.signal_type === "BUY" ? UP : DOWN,
      text: s.signal_type,
    }));
    renderMarkers();
    chart.current?.timeScale().scrollToRealTime();
  }, [payload]);

  useEffect(() => {
    if (!liveUpdate || !payload || liveUpdate.symbol !== payload.symbol || liveUpdate.timeframe !== payload.timeframe) return;
    candles.current?.update(candleBar(liveUpdate.candle));
    volume.current?.update(volumeBar(liveUpdate.candle));
    const p = liveUpdate.point;
    if (p) {
      const op = overlayPoint(payload.indicator?.name, p);
      if (op) overlay.current?.update(op);
      if (p.signal) {
        signalMarkers.current.push({
          time: t(p.time),
          position: p.signal === "BUY" ? "belowBar" : "aboveBar",
          shape: p.signal === "BUY" ? "arrowUp" : "arrowDown",
          color: p.signal === "BUY" ? UP : DOWN,
          text: p.signal,
        });
        renderMarkers();
      }
    }
  }, [liveUpdate, payload]);

  useEffect(() => {
    const snap = (ms: number) => t(Math.floor(ms / timeframeMs) * timeframeMs);
    const sym = payload?.symbol;
    const out: SeriesMarker<Time>[] = [];
    for (const tr of trades) {
      if (tr.symbol !== sym) continue;
      if (tr.entry_ts >= firstCandle.current)
        out.push({ time: snap(tr.entry_ts), position: "belowBar", shape: "circle", color: "#38bdf8", text: `Entry ${Number(tr.entry_price).toFixed(2)}` });
      if (tr.exit_ts && tr.exit_ts >= firstCandle.current)
        out.push({ time: snap(tr.exit_ts), position: "aboveBar", shape: "circle", color: "#f59e0b", text: `Exit ${Number(tr.exit_price).toFixed(2)}` });
    }
    tradeMarkers.current = out;
    renderMarkers();
  }, [trades, payload, timeframeMs]);

  return <div ref={el} className="h-full w-full" />;
}
