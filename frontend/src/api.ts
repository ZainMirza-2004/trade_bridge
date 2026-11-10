async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* non-JSON error */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body: unknown = {}) => request<T>("POST", path, body),
};

export type Handler = (type: string, data: unknown) => void;

export function connectSocket(onEvent: Handler, onState: (connected: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let ping: ReturnType<typeof setInterval> | undefined;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      onState(true);
      ping = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 20000);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      onEvent(msg.type, msg.data);
    };
    ws.onclose = () => {
      onState(false);
      clearInterval(ping);
      if (!closed) retry = setTimeout(open, 2000);
    };
  };
  open();
  return () => {
    closed = true;
    clearTimeout(retry);
    clearInterval(ping);
    ws?.close();
  };
}

export const fmt = (v: string | number | null | undefined, dp = 2): string => {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
};

export const fmtQty = (v: string | null | undefined) => (v ? String(Number(v)) : "—");
export const fmtTime = (ms: number | null | undefined) => (ms ? new Date(ms).toLocaleString() : "—");
export const pnlClass = (v: string | null | undefined) => (!v ? "" : Number(v) > 0 ? "text-emerald-400" : Number(v) < 0 ? "text-rose-400" : "");
