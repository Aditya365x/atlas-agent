"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api, ApiError, auth, streamRun,
  type AgentEvent, type Conversation, type Doc, type Me, type Message, type Stats,
} from "@/lib/api";
import Logo from "./Logo";
import { AssistantMessage, UserMessage, type AssistantView } from "./Message";

const STARTERS = [
  { title: "Compare the three cheapest ways to run Llama 3 70B in production", hint: "Searches, reads pricing pages, builds a table" },
  { title: "Simulate 10,000 rolls of two dice and chart the distribution of sums", hint: "Writes and runs Python, returns a chart" },
  { title: "What changed in the latest stable Python release?", hint: "Reads the official release notes and cites them" },
  { title: "Summarize my uploaded document and list open questions", hint: "Searches the files you attach with the clip" },
];

type Live = { runId: string; userText: string; view: AssistantView };

const emptyView = (): AssistantView => ({ content: "", events: [], images: [], sources: [], live: true });

function applyEvent(v: AssistantView, e: AgentEvent): AssistantView {
  switch (e.type) {
    case "token":
      return { ...v, content: v.content + e.text };
    case "answer.reset":
      return { ...v, content: "", events: [...v.events, e] };
    case "tool.result":
      return {
        ...v,
        events: [...v.events, e],
        images: [...v.images, ...(e.images ?? [])],
        sources: [...v.sources, ...(e.sources ?? []).filter((s) => s.url && !v.sources.some((x) => x.url === s.url))],
      };
    case "plan":
    case "tool.call":
    case "reflection":
    case "guard.flag":
      return { ...v, events: [...v.events, e] };
    case "run.completed":
      return { ...v, content: e.answer, usage: e.usage, latencyMs: e.latency_ms, live: false };
    case "run.failed":
      return { ...v, live: false, error: e.error || "The run failed. Try again." };
    case "run.cancelled":
      return { ...v, live: false, cancelled: true };
    default:
      return v;
  }
}

function viewFromMessage(m: Message): AssistantView {
  return {
    content: m.content,
    events: m.meta.trace ?? [],
    images: m.meta.images ?? [],
    sources: m.meta.sources ?? [],
    usage: m.meta.usage,
    latencyMs: m.meta.latency_ms,
    live: false,
  };
}

export default function ChatApp({ onLogout }: { onLogout: () => void }) {
  const [me, setMe] = useState<Me | null>(null);
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [files, setFiles] = useState<Doc[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [input, setInput] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [sideOpen, setSideOpen] = useState(false);
  const [stats, setStats] = useState<Stats | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const statsRef = useRef<HTMLDialogElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottom = useRef(true);

  const flash = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 5000);
  };

  const refreshSide = useCallback(async () => {
    const [c, f, m] = await Promise.all([api<Conversation[]>("/v1/conversations"), api<Doc[]>("/v1/files"), api<Me>("/v1/me")]);
    setConvs(c);
    setFiles(f);
    setMe(m);
  }, []);

  const follow = useCallback(async (runId: string, userText: string, conversationId: string) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLive({ runId, userText, view: emptyView() });
    let final: AgentEvent | null = null;
    try {
      await streamRun(runId, (e) => {
        if (e.type === "run.completed" || e.type === "run.failed" || e.type === "run.cancelled") final = e;
        setLive((l) => (l && l.runId === runId ? { ...l, view: applyEvent(l.view, e) } : l));
      }, ctrl.signal);
    } catch (err) {
      setLive((l) => (l ? { ...l, view: { ...l.view, live: false, error: err instanceof ApiError ? err.message : "Stream failed" } } : l));
    }
    if (ctrl.signal.aborted) return;
    if ((final as AgentEvent | null)?.type === "run.completed") {
      const conv = await api<{ messages: Message[] }>(`/v1/conversations/${conversationId}`);
      setMessages(conv.messages);
      setLive(null);
    }
    refreshSide().catch(() => {});
  }, [refreshSide]);

  const openConversation = useCallback(async (id: string | null) => {
    abortRef.current?.abort();
    setLive(null);
    setActiveId(id);
    setSideOpen(false);
    if (!id) {
      setMessages([]);
      textRef.current?.focus();
      return;
    }
    const conv = await api<{ messages: Message[] }>(`/v1/conversations/${id}`);
    const msgs = conv.messages;
    const last = msgs[msgs.length - 1];
    // Resume a run that is still going (e.g. after a refresh or on another device)
    if (last?.role === "user" && last.run_id) {
      const run = await api<{ status: string }>(`/v1/runs/${last.run_id}`);
      if (run.status === "queued" || run.status === "running") {
        setMessages(msgs.slice(0, -1));
        follow(last.run_id, last.content, id);
        return;
      }
    }
    setMessages(msgs);
  }, [follow]);

  useEffect(() => {
    refreshSide().catch((e) => flash(e.message));
    return () => abortRef.current?.abort();
  }, [refreshSide]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages, live]);

  async function send(text: string) {
    const msg = text.trim();
    if (!msg || (live && live.view.live)) return;
    setInput("");
    stickToBottom.current = true;
    try {
      const res = await api<{ run_id: string; conversation_id: string }>("/v1/runs", {
        method: "POST",
        body: JSON.stringify({ message: msg, conversation_id: activeId }),
      });
      if (!activeId) {
        setActiveId(res.conversation_id);
        setMessages([]);
      } else if (live) {
        // keep the finished (failed/cancelled) live block in the thread before starting the next run
        const conv = await api<{ messages: Message[] }>(`/v1/conversations/${activeId}`);
        setMessages(conv.messages);
      }
      refreshSide().catch(() => {});
      follow(res.run_id, msg, res.conversation_id);
    } catch (e) {
      setInput(msg);
      flash(e instanceof ApiError ? e.message : "Couldn't start the run.");
    }
  }

  async function stop() {
    if (!live) return;
    await api(`/v1/runs/${live.runId}/cancel`, { method: "POST" }).catch(() => {});
  }

  async function upload(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    flash(`Reading ${file.name}…`);
    try {
      const doc = await api<Doc>("/v1/files", { method: "POST", body: fd });
      flash(`Added ${doc.filename} (${doc.num_chunks} passages). Ask about it anytime.`);
      refreshSide().catch(() => {});
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Upload failed.");
    }
  }

  async function removeConversation(id: string) {
    await api(`/v1/conversations/${id}`, { method: "DELETE" });
    if (id === activeId) openConversation(null);
    refreshSide().catch(() => {});
  }

  async function showStats() {
    setStats(await api<Stats>("/v1/stats"));
    statsRef.current?.showModal();
  }

  const running = !!live?.view.live;
  const spendPct = me ? Math.min(100, (me.spend_24h_usd / Math.max(me.daily_limit_usd, 0.0001)) * 100) : 0;
  const isEmpty = !messages.length && !live;

  return (
    <div className="shell">
      <aside className={`sidebar ${sideOpen ? "open" : ""}`} aria-label="Conversations and files">
        <div className="brand"><Logo /> Atlas</div>
        <div className="side-section">
          <button className="btn" style={{ width: "100%" }} onClick={() => openConversation(null)}>New task</button>
        </div>
        <div className="side-heading" style={{ padding: "12px 18px 4px" }}>Recent</div>
        <nav className="conv-list">
          {convs.length === 0 && <div className="file-row">Your tasks will show up here.</div>}
          {convs.map((c) => (
            <div key={c.id} className={`conv ${c.id === activeId ? "active" : ""}`}>
              <button className="conv" style={{ padding: 0 }} onClick={() => openConversation(c.id)} title={c.title}>
                <span>{c.title}</span>
              </button>
              <button className="del" aria-label={`Delete ${c.title}`} onClick={() => removeConversation(c.id)}>×</button>
            </div>
          ))}
        </nav>
        <div className="side-section" style={{ borderTop: "1px solid var(--line)" }}>
          <div className="side-heading">Your files</div>
          {files.length === 0 && <div className="file-row">Attach PDFs or notes with the clip.</div>}
          {files.slice(0, 6).map((f) => (
            <div key={f.id} className="file-row" title={f.filename}>
              <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden><path d="M4 1h5l4 4v10H4z" fill="none" stroke="currentColor" strokeWidth="1.3" /></svg>
              <span>{f.filename}</span>
            </div>
          ))}
        </div>
        <div className="side-foot">
          {me && (
            <>
              <div className="foot-row"><span>Today&apos;s usage</span><span>${me.spend_24h_usd.toFixed(3)} of ${me.daily_limit_usd.toFixed(2)}</span></div>
              <div className="meter" role="progressbar" aria-valuenow={Math.round(spendPct)} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${spendPct}%` }} /></div>
            </>
          )}
          <div className="foot-row">
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{auth.email}</span>
            <span>
              <button className="btn btn-ghost" style={{ padding: "4px 6px" }} onClick={showStats}>Stats</button>
              <button className="btn btn-ghost" style={{ padding: "4px 6px" }} onClick={() => { auth.clear(); onLogout(); }}>Sign out</button>
            </span>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <button className="btn btn-ghost" aria-label="Open menu" onClick={() => setSideOpen(true)}>☰</button>
          <Logo size={22} /> Atlas
        </div>
        <div
          className="scroller"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          <div className="thread" aria-live="polite">
            {isEmpty && (
              <div className="empty">
                <h1>What should Atlas get done?</h1>
                <p className="lead">Hand over research, analysis or number crunching. Atlas picks the tools, checks its work, and shows the route it took.</p>
                <div className="starters">
                  {STARTERS.map((s) => (
                    <button key={s.title} className="starter" onClick={() => send(s.title)}>
                      {s.title}
                      <small>{s.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m) =>
              m.role === "user" ? <UserMessage key={m.id} text={m.content} /> : <AssistantMessage key={m.id} view={viewFromMessage(m)} />,
            )}
            {live && (
              <>
                <UserMessage text={live.userText} />
                <AssistantMessage view={live.view} />
              </>
            )}
          </div>
        </div>

        <div className="composer-wrap">
          {toast && <div className="toast" role="status">{toast}</div>}
          <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
            <input ref={fileRef} type="file" hidden accept=".pdf,.txt,.md,.csv,.json,.py,.html,.log"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} />
            <button type="button" className="btn btn-ghost icon-btn" aria-label="Attach a file" onClick={() => fileRef.current?.click()}>
              <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden><path d="M21 11.5l-8.6 8.6a5 5 0 01-7.1-7.1l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>
            </button>
            <label className="sr-only" htmlFor="task">Task for Atlas</label>
            <textarea
              id="task"
              ref={textRef}
              rows={1}
              placeholder="Give Atlas a task"
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                e.target.style.height = "auto";
                e.target.style.height = `${e.target.scrollHeight}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send(input);
                }
              }}
            />
            {running ? (
              <button type="button" className="btn icon-btn" aria-label="Stop" onClick={stop}>
                <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><rect x="2" y="2" width="10" height="10" rx="2" fill="currentColor" /></svg>
              </button>
            ) : (
              <button className="btn btn-primary icon-btn" aria-label="Send" disabled={!input.trim()}>
                <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden><path d="M8 13V3M3.5 7.5L8 3l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </button>
            )}
          </form>
          <div className="composer-hint">Enter to send, Shift+Enter for a new line. Atlas can make mistakes; check the route and sources.</div>
        </div>
      </main>

      <dialog ref={statsRef} onClick={(e) => e.target === statsRef.current && statsRef.current?.close()}>
        <h2 style={{ margin: "0 0 2px", fontSize: 18 }}>Your runs</h2>
        <div style={{ color: "var(--ink-3)", fontSize: 13 }}>Last 1,000 tasks</div>
        {stats && (
          <div className="stats-grid">
            <div className="stat"><b>{stats.runs}</b><span>tasks run</span></div>
            <div className="stat"><b>{stats.runs ? Math.round((stats.completed / stats.runs) * 100) : 0}%</b><span>completed</span></div>
            <div className="stat"><b>{stats.avg_latency_ms ? (stats.avg_latency_ms / 1000).toFixed(1) + "s" : "n/a"}</b><span>average time</span></div>
            <div className="stat"><b>{stats.p95_latency_ms ? (stats.p95_latency_ms / 1000).toFixed(1) + "s" : "n/a"}</b><span>p95 time</span></div>
            <div className="stat"><b>{stats.tool_calls}</b><span>tool calls</span></div>
            <div className="stat"><b>${stats.total_cost_usd.toFixed(4)}</b><span>model spend</span></div>
          </div>
        )}
        <div style={{ textAlign: "right", marginTop: 16 }}>
          <button className="btn" onClick={() => statsRef.current?.close()}>Close</button>
        </div>
      </dialog>
    </div>
  );
}
