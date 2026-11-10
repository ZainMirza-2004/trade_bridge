export type Mode = "PAPER" | "LIVE";

export interface CandleData { time: number; open: number; high: number; low: number; close: number; volume: number }
export interface IndicatorPointData { time: number; values: Record<string, number | null>; signal: "BUY" | "SELL" | null }
export interface ChartSignal { indicator: string; symbol: string; timeframe: string; signal_type: "BUY" | "SELL"; candle_ts: number; price: number }

export interface ParamSpec { name: string; label: string; type: "int" | "float" | "bool"; default: number | boolean; min: number | null; max: number | null; step: number | null; description: string }
export interface IndicatorMeta { name: string; label: string; description: string; params: ParamSpec[]; overlay_keys: string[] }

export interface ChartPayload {
  symbol: string; timeframe: string; indicator: IndicatorMeta | null; parameters: Record<string, unknown>;
  candles: CandleData[]; points: IndicatorPointData[]; signals: ChartSignal[];
}

export interface TradingConfig {
  indicator: string; symbol: string; timeframe: string; parameters: Record<string, number | boolean>;
  position_size_type: "fixed" | "percent"; position_size_value: string;
}

export interface Risk { max_position_size: string | null; max_daily_loss: string | null; max_open_positions: number; min_balance: string | null }

export interface TradingStatus {
  mode: Mode; running: boolean; paused: boolean; emergency_stop: boolean; blocked_reason: string | null; last_skip_reason: string | null;
  connection_status: string; connection_error: string | null; credentials_configured: boolean; live_ready: boolean;
  live_confirmation_phrase: string; market_data_stale: boolean; market_data_error: string | null; last_market_update: number | null;
  reconciliation_warnings: string[]; config: TradingConfig | null; risk: Risk;
  market_data_status: "LIVE" | "STALE" | "DISCONNECTED"; poll_interval_seconds: number; stale_data_seconds: number;
  live_check: LiveCheck | null;
}

export interface LiveCheck { ok: boolean; checked_at: number; symbol: string; errors: string[]; warnings: string[];
}

export interface Ticker { symbol: string; bid: string; ask: string; mid: string; last_price: string; high_24h: string; low_24h: string; price_change_24h: string; volume_24h: string }

export interface Trade {
  id: number; mode: Mode; indicator: string; symbol: string; side: string; status: "OPEN" | "CLOSED";
  entry_price: string; exit_price: string | null; quantity: string; open_quantity: string; entry_cost: string; exit_proceeds: string;
  fees: string; realized_pnl: string; realized_pnl_pct: string | null; unrealized_pnl: string | null;
  entry_ts: number; exit_ts: number | null; entry_signal_ts: number | null; exit_signal_ts: number | null; duration_seconds: number | null;
}

export interface Order {
  id: number; client_order_id: string; exchange_order_id: string | null; mode: Mode; symbol: string; side: string; status: string;
  requested_quantity: string | null; requested_quote_amount: string | null; filled_quantity: string; average_fill_price: string | null;
  fees: string; fee_currency: string | null; reject_reason: string | null; created_at: number;
}

export interface SignalRecord {
  id: number; indicator: string; symbol: string; timeframe: string; signal_type: "BUY" | "SELL"; signal_price: string;
  candle_ts: number; mode: Mode; execution_status: string; reason: string | null; created_at: number;
}

export interface Performance {
  mode: Mode; total_trades: number; winning_trades: number; losing_trades: number; win_rate: string | null;
  total_realized_pnl: string; unrealized_pnl: string; average_win: string | null; average_loss: string | null;
  profit_factor: string | null; max_drawdown: string; total_fees: string; open_positions: number; realized_pnl_today: string | null;
}

export interface Pair { symbol: string; base: string; quote: string; min_order_size: string; min_order_size_quote: string; status: string }
export interface Balances { mode: Mode; balances: Record<string, string>; error?: string }

export interface CandleUpdate { symbol: string; timeframe: string; candle: CandleData; closed: boolean; point: IndicatorPointData | null }
