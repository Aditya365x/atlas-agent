export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

const TOKEN_KEY = "atlas.token";
const EMAIL_KEY = "atlas.email";

export type Image = { mime: string; b64: string; name?: string };
export type Source = { title: string; url: string };
export type Usage = { prompt_tokens?: number; completion_tokens?: number; cost_usd?: number; models?: string[]; ttft_s?: number };

export type AgentEvent =
  | { type: "run.started"; run_id: string }
  | { type: "plan"; steps: string[]; needs_tools: boolean }
  | { type: "step.started"; step: number; phase: string }
  | { type: "token"; text: string }
  | { type: "answer.reset"; reason: string; note?: string }
  | { type: "tool.call"; id: string; tool: string; args: Record<string, unknown> }
  | { type: "tool.result"; id: string; tool: string; ok: boolean; ms: number; preview: string; images?: Image[]; sources?: Source[] }
  | { type: "reflection"; score: number; feedback: string; revise: boolean }
  | { type: "guard.flag"; tool: string; flags: string[] }
  | { type: "run.completed"; message_id: string; answer: string; usage: Usage; latency_ms: number; steps: number; tool_calls: number }
  | { type: "run.failed"; error: string }
  | { type: "run.cancelled" };

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  run_id?: string | null;
  created_at: string;
  meta: { trace?: AgentEvent[]; images?: Image[]; sources?: Source[]; usage?: Usage; latency_ms?: number; stop_reason?: string };
};
export type Conversation = { id: string; title: string; updated_at: string };
export type Doc = { id: string; filename: string; num_chunks: number; created_at: string };
export type Me = { id: string; email: string; spend_24h_usd: number; daily_limit_usd: number };
export type Stats = { runs: number; completed: number; failed: number; avg_latency_ms: number | null; p95_latency_ms: number | null; total_cost_usd: number; total_tokens: number; tool_calls: number };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export const auth = {
  get token() {
    return typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY);
  },
  get email() {
    return typeof window === "undefined" ? null : localStorage.getItem(EMAIL_KEY);
  },
  save(token: string, email: string) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(EMAIL_KEY, email);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(EMAIL_KEY);
  },
};

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (auth.token) headers.set("Authorization", `Bearer ${auth.token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the Atlas API. Check that the backend is running.");
  }
  if (res.status === 401 && auth.token) {
    auth.clear();
    window.dispatchEvent(new Event("atlas:logout"));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : body.detail?.[0]?.msg ?? detail;
    } catch {}
    const retry = res.headers.get("Retry-After");
    throw new ApiError(res.status, retry ? `${detail} (retry in ${retry}s)` : detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const TERMINAL = new Set(["run.completed", "run.failed", "run.cancelled"]);

/** Streams a run's events over SSE (fetch-based so we can send the auth header).
 *  Reconnects with Last-Event-ID on network drops, so no token is lost or duplicated. */
export async function streamRun(runId: string, onEvent: (e: AgentEvent) => void, signal: AbortSignal) {
  let lastId: string | null = null;
  for (let attempt = 0; attempt < 6 && !signal.aborted; attempt++) {
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${auth.token}`, Accept: "text/event-stream" };
      if (lastId) headers["Last-Event-ID"] = lastId;
      const res = await fetch(`${API_URL}/v1/runs/${runId}/events`, { headers, signal });
      if (!res.ok || !res.body) throw new ApiError(res.status, "stream failed");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("id:")) lastId = line.slice(3).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (!data) continue; // keep-alive ping
          const ev = JSON.parse(data) as AgentEvent;
          onEvent(ev);
          if (TERMINAL.has(ev.type)) return;
        }
      }
    } catch (err) {
      if (signal.aborted) return;
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) throw err;
    }
    await new Promise((r) => setTimeout(r, Math.min(4000, 400 * 2 ** attempt)));
  }
  if (!signal.aborted) onEvent({ type: "run.failed", error: "Lost connection to the run. Refresh to see the result." });
}
