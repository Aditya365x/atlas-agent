"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Attachment01Icon, File02Icon } from "@hugeicons/core-free-icons";
import {
  api, ApiError, API_URL, auth, streamRun,
  type AgentEvent, type Conversation, type Doc, type Effort, type Info, type Me, type Message, type Stats,
} from "@/lib/api";
import BlurText from "./bits/BlurText";
import PromptBar, { type PromptBarSource } from "./bits/PromptBar";
import SpotlightCard from "./bits/SpotlightCard";
import Logo from "./Logo";
import { AssistantMessage, UserMessage, type AssistantView } from "./Message";

type Suggestion = { tag: string; title: string; hint: string; prompt: string; sample?: string; effort?: Effort };

const SUGGESTIONS: Suggestion[] = [
  {
    tag: "Data analysis",
    title: "Analyse a sales dataset",
    hint: "Loads sales.csv, runs pandas, charts revenue by region",
    prompt: "Using my uploaded sales.csv, chart monthly revenue by region, tell me which region earned the most in total and which grew fastest from January to the latest month.",
    sample: "sales.csv",
  },
  {
    tag: "Support ops",
    title: "Find what hurts customer satisfaction",
    hint: "240 tickets: CSAT and resolution time by category",
    prompt: "In my uploaded support-tickets.csv, which ticket categories have the lowest average CSAT and the slowest resolution? Show a short table and recommend two fixes.",
    sample: "support-tickets.csv",
  },
  {
    tag: "Company RAG",
    title: "Ask the employee handbook",
    hint: "Hybrid BM25 + vector search over policy docs, with citations",
    prompt: "According to my uploaded Northwind handbook, what approval does a 20,000 INR refund need, and can I fly business class on a 5 hour flight?",
    sample: "northwind-handbook.md",
  },
  {
    tag: "Web research",
    title: "Research with live sources",
    hint: "Searches, reads pages, cites every claim",
    prompt: "What are the three most important changes in the latest stable Python release? Cite the official sources.",
  },
];

const SAMPLES = [
  { name: "sales.csv", label: "Sales data" },
  { name: "support-tickets.csv", label: "Support tickets" },
  { name: "northwind-handbook.md", label: "Handbook" },
  { name: "northwind-api-reference.md", label: "API docs" },
];

const COMMANDS = [
  { key: "research", name: "/research", description: "Search the web and cite sources", expand: "Research this thoroughly on the web, read the best sources and cite them:" },
  { key: "analyze", name: "/analyze", description: "Analyse my uploaded data with Python", expand: "Analyse my uploaded data with Python (pandas) and report concrete numbers:" },
  { key: "chart", name: "/chart", description: "Make a chart", expand: "Write and run Python to make a clear, labelled chart for:" },
  { key: "docs", name: "/docs", description: "Answer from my documents", expand: "Answer only from my uploaded documents, quoting the relevant passages:" },
  { key: "compare", name: "/compare", description: "Side-by-side comparison table", expand: "Compare the following in a concise Markdown table, then give a recommendation:" },
];

const EFFORTS: { label: string; value: Effort }[] = [
  { label: "Fast", value: "fast" },
  { label: "Balanced", value: "balanced" },
  { label: "Deep", value: "deep" },
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
    case "context.compacted":
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

function expandCommand(text: string): string {
  const m = /^\/(\w+)\s*([\s\S]*)$/.exec(text.trim());
  const cmd = m && COMMANDS.find((c) => c.key === m[1]);
  return cmd ? `${cmd.expand} ${m![2]}`.trim() : text;
}

function groupByDate(convs: Conversation[]) {
  const now = Date.now();
  const day = 86_400_000;
  const groups: [string, Conversation[]][] = [["Today", []], ["Previous 7 days", []], ["Older", []]];
  for (const c of convs) {
    const age = now - new Date(c.updated_at).getTime();
    groups[age < day ? 0 : age < 7 * day ? 1 : 2][1].push(c);
  }
  return groups.filter(([, list]) => list.length);
}

const Icon = {
  side: <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden><rect x="3" y="4" width="18" height="16" rx="3" fill="none" stroke="currentColor" strokeWidth="1.7" /><path d="M9 4v16" stroke="currentColor" strokeWidth="1.7" /></svg>,
  edit: <svg width="17" height="17" viewBox="0 0 24 24" aria-hidden><path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  search: <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
  plug: <svg width="17" height="17" viewBox="0 0 24 24" aria-hidden><path d="M9 7V3M15 7V3M6 7h12v4a6 6 0 01-12 0zM12 17v4" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>,
  chart: <svg width="17" height="17" viewBox="0 0 24 24" aria-hidden><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>,
  out: <svg width="17" height="17" viewBox="0 0 24 24" aria-hidden><path d="M15 4h3a2 2 0 012 2v12a2 2 0 01-2 2h-3M10 16l-4-4 4-4M6 12h10" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  x: <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>,
  file: <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden><path d="M4 1.5h5l3.5 3.5v9.5h-8.5z" fill="none" stroke="currentColor" strokeWidth="1.3" /></svg>,
};

export default function ChatApp({ onLogout }: { onLogout: () => void }) {
  const [me, setMe] = useState<Me | null>(null);
  const [info, setInfo] = useState<Info | null>(null);
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [files, setFiles] = useState<Doc[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [effort, setEffort] = useState<Effort>("balanced");
  const [prefill, setPrefill] = useState<{ text: string; id: number } | undefined>();
  const [stats, setStats] = useState<Stats | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pendingUpload = useRef<((name: string | undefined) => void) | null>(null);
  const statsRef = useRef<HTMLDialogElement>(null);
  const mcpRef = useRef<HTMLDialogElement>(null);
  const stickToBottom = useRef(true);
  const lastPrompt = useRef<string>("");

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 4000);
  }, []);

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
    setMobileOpen(false);
    if (!id) {
      setMessages([]);
      return;
    }
    const conv = await api<{ messages: Message[] }>(`/v1/conversations/${id}`);
    const msgs = conv.messages;
    const last = msgs[msgs.length - 1];
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
    api<Info>("/v1/info").then(setInfo).catch(() => {});
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o") {
        e.preventDefault();
        openConversation(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      abortRef.current?.abort();
    };
  }, [refreshSide, flash, openConversation]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages, live]);

  async function send(raw: string, attachments: string[] = [], effortOverride?: Effort) {
    const text = raw.trim();
    if (!text || live?.view.live) return;
    const msg = expandCommand(text) + (attachments.length ? `\n\n(Attached: ${attachments.join(", ")})` : "");
    lastPrompt.current = msg;
    stickToBottom.current = true;
    try {
      const res = await api<{ run_id: string; conversation_id: string }>("/v1/runs", {
        method: "POST",
        body: JSON.stringify({ message: msg, conversation_id: activeId, effort: effortOverride ?? effort }),
      });
      if (!activeId) {
        setActiveId(res.conversation_id);
        setMessages([]);
      } else if (live) {
        const conv = await api<{ messages: Message[] }>(`/v1/conversations/${activeId}`);
        setMessages(conv.messages);
      }
      refreshSide().catch(() => {});
      follow(res.run_id, msg, res.conversation_id);
    } catch (e) {
      setPrefill({ text, id: Date.now() });
      flash(e instanceof ApiError ? e.message : "Couldn't start the run.");
    }
  }

  async function loadSample(name: string, quiet = false) {
    try {
      await api<Doc>(`/v1/samples/${encodeURIComponent(name)}`, { method: "POST" });
      if (!quiet) flash(`Added ${name} to your files`);
      refreshSide().catch(() => {});
      return true;
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Couldn't load the sample.");
      return false;
    }
  }

  async function runSuggestion(s: Suggestion) {
    if (s.sample && !files.some((f) => f.filename === s.sample)) {
      flash(`Loading ${s.sample}…`);
      if (!(await loadSample(s.sample, true))) return;
    }
    send(s.prompt, [], s.effort);
  }

  async function uploadFile(file: File): Promise<string | undefined> {
    const fd = new FormData();
    fd.append("file", file);
    flash(`Reading ${file.name}…`);
    try {
      const doc = await api<Doc>("/v1/files", { method: "POST", body: fd });
      flash(`Added ${doc.filename}`);
      refreshSide().catch(() => {});
      return doc.filename;
    } catch (e) {
      flash(e instanceof ApiError ? e.message : "Upload failed.");
      return undefined;
    }
  }

  function pickFile(): Promise<string | undefined> {
    return new Promise((resolve) => {
      pendingUpload.current = resolve;
      fileRef.current?.click();
    });
  }

  function dictate(): Promise<string | undefined> {
    type SR = { lang: string; interimResults: boolean; onresult: (e: { results: { 0: { transcript: string } }[] }) => void; onerror: () => void; onend: () => void; start: () => void };
    const w = window as unknown as { SpeechRecognition?: new () => SR; webkitSpeechRecognition?: new () => SR };
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!Ctor) {
      flash("Voice input isn't supported in this browser.");
      return Promise.resolve(undefined);
    }
    return new Promise((resolve) => {
      const rec = new Ctor();
      let text = "";
      rec.lang = navigator.language || "en-US";
      rec.interimResults = false;
      rec.onresult = (e) => { text = Array.from(e.results).map((r) => r[0].transcript).join(" "); };
      rec.onerror = () => resolve(undefined);
      rec.onend = () => resolve(text || undefined);
      rec.start();
    });
  }

  async function stop() {
    if (live) await api(`/v1/runs/${live.runId}/cancel`, { method: "POST" }).catch(() => {});
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

  const sources: PromptBarSource[] = useMemo(
    () => [
      { key: "__upload", name: "Upload a file", description: "PDF, CSV, XLSX, MD, TXT", icon: Attachment01Icon, attach: true },
      ...files.slice(0, 8).map((f) => ({ key: f.id, name: f.filename, description: "Your file", icon: File02Icon })),
    ],
    [files],
  );

  const running = !!live?.view.live;
  const isEmpty = !messages.length && !live;
  const filtered = query ? convs.filter((c) => c.title.toLowerCase().includes(query.toLowerCase())) : convs;
  const spendPct = me ? Math.min(100, (me.spend_24h_usd / Math.max(me.daily_limit_usd, 0.0001)) * 100) : 0;
  const isGuest = me?.email.endsWith("@guest.atlas");
  const mcpUrl = `${API_URL}${info?.mcp_server ?? "/mcp"}`;

  const composer = (
    <div className="composer">
      <PromptBar
        placeholder={isEmpty ? "Ask Atlas to research, analyse or build something" : "Ask a follow-up"}
        sources={sources}
        commands={COMMANDS}
        models={[]}
        efforts={EFFORTS.map((e) => e.label)}
        defaultEffort="Balanced"
        onEffortChange={(label) => setEffort(EFFORTS.find((e) => e.label === label)?.value ?? "balanced")}
        busy={running}
        onSend={(text, detail) => send(text, detail.attachments)}
        onStop={stop}
        onAttach={async () => (await pickFile()) ?? undefined}
        onDictate={async () => (await dictate()) ?? undefined}
        background="#141414"
        menuBackground="#1c1c1c"
        color="#f4f4f4"
        sparkColor="#ffffff"
        width={780}
        radius={26}
        maxRows={8}
        prefill={prefill}
      />
    </div>
  );

  return (
    <div className={`shell ${collapsed ? "collapsed" : ""} ${mobileOpen ? "mobile-open" : ""}`}>
      <div className="scrim" onClick={() => setMobileOpen(false)} />
      <aside className="sidebar" aria-label="Conversations and files">
        <div className="side-top">
          <div className="brand"><Logo size={24} /> Atlas</div>
          <button className="btn btn-ghost icon-btn" aria-label="Close sidebar" onClick={() => (window.innerWidth < 860 ? setMobileOpen(false) : setCollapsed(true))}>{Icon.side}</button>
        </div>
        <div className="side-actions">
          <button className="side-item" onClick={() => openConversation(null)}>{Icon.edit} New chat <span className="kbd">Ctrl⇧O</span></button>
          <button className="side-item" onClick={() => mcpRef.current?.showModal()}>{Icon.plug} Connect via MCP</button>
          <button className="side-item" onClick={showStats}>{Icon.chart} Usage and stats</button>
        </div>
        <label className="side-search">
          {Icon.search}
          <span className="sr-only">Search chats</span>
          <input placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <nav className="side-scroll">
          {filtered.length === 0 && <div className="side-empty">{query ? "No chats match." : "Your chats will appear here."}</div>}
          {groupByDate(filtered).map(([label, list]) => (
            <div className="side-group" key={label}>
              <div className="side-label">{label}</div>
              {list.map((c) => (
                <div key={c.id} className={`conv ${c.id === activeId ? "active" : ""}`}>
                  <button className="open" onClick={() => openConversation(c.id)} title={c.title}>{c.title}</button>
                  <button className="del" aria-label={`Delete ${c.title}`} onClick={() => removeConversation(c.id)}>{Icon.x}</button>
                </div>
              ))}
            </div>
          ))}
          <div className="side-group">
            <div className="side-label">Files</div>
            {files.length === 0 && <div className="side-empty">Upload with + in the composer, or load a sample.</div>}
            {files.slice(0, 8).map((f) => (
              <div key={f.id} className="file-row" title={f.filename}>{Icon.file}<span>{f.filename}</span></div>
            ))}
          </div>
        </nav>
        <div className="side-foot">
          {me && (
            <div className="usage">
              <div className="usage-row"><span>Today&apos;s budget</span><span>${me.spend_24h_usd.toFixed(3)} / ${me.daily_limit_usd.toFixed(2)}</span></div>
              <div className="meter" role="progressbar" aria-valuenow={Math.round(spendPct)} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${spendPct}%` }} /></div>
            </div>
          )}
          <div className="account">
            <div className="avatar">{isGuest ? "G" : (auth.email ?? "?")[0].toUpperCase()}</div>
            <div className="account-name">{isGuest ? "Guest session" : auth.email}</div>
            <button className="btn btn-ghost icon-btn" aria-label="Sign out" title="Sign out" onClick={() => { auth.clear(); onLogout(); }}>{Icon.out}</button>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <button
            className="btn btn-ghost icon-btn toggle-side"
            aria-label="Open sidebar"
            onClick={() => (window.innerWidth < 860 ? setMobileOpen(true) : setCollapsed(false))}
          >
            {Icon.side}
          </button>
          <button className="model-chip" onClick={() => mcpRef.current?.showModal()} title="About this deployment">
            Atlas <small>{info?.model ?? ""}</small>
          </button>
          <span className="spacer" />
          {running && <span className="pill"><span className="live-dot" /> {EFFORTS.find((e) => e.value === effort)?.label} mode</span>}
          {!isEmpty && <button className="btn btn-ghost icon-btn" aria-label="New chat" title="New chat" onClick={() => openConversation(null)}>{Icon.edit}</button>}
        </div>

        <div
          className="scroller"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
        >
          {isEmpty ? (
            <section className="hero">
              <div className="hero-h" role="heading" aria-level={1}><BlurText key={activeId ?? "new"} text="What can I help with?" delay={70} animateBy="words" direction="bottom" /></div>
              <p className="sub">Atlas plans, uses tools, checks its own work and shows you every step.</p>
              {composer}
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <SpotlightCard key={s.title} className="suggestion" spotlightColor="rgba(255, 255, 255, 0.10)" theme="dark">
                    <button onClick={() => runSuggestion(s)}>
                      <span className="tag">{s.tag}</span>
                      <b>{s.title}</b>
                      <span>{s.hint}</span>
                    </button>
                  </SpotlightCard>
                ))}
              </div>
              <div className="samples">
                <span>Sample files:</span>
                {SAMPLES.map((s) => {
                  const loaded = files.some((f) => f.filename === s.name);
                  return (
                    <button key={s.name} className="chip" data-on={loaded ? "" : undefined} onClick={() => !loaded && loadSample(s.name)}>
                      {loaded ? "✓ " : "+ "}{s.label}
                    </button>
                  );
                })}
              </div>
            </section>
          ) : (
            <div className="thread" aria-live="polite">
              {messages.map((m) =>
                m.role === "user" ? <UserMessage key={m.id} text={m.content} /> : <AssistantMessage key={m.id} view={viewFromMessage(m)} />,
              )}
              {live && (
                <>
                  <UserMessage text={live.userText} />
                  <AssistantMessage view={live.view} onRetry={live.view.error ? () => send(lastPrompt.current) : undefined} />
                </>
              )}
            </div>
          )}
        </div>

        {!isEmpty && (
          <div className="dock">
            {composer}
            <div className="hint">Atlas can make mistakes. Open the steps to check its work and sources.</div>
          </div>
        )}
        {toast && <div className="toast" role="status">{toast}</div>}
        <input
          ref={fileRef}
          type="file"
          hidden
          accept=".pdf,.txt,.md,.csv,.xlsx,.json,.py,.html,.log"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            const name = f ? await uploadFile(f) : undefined;
            pendingUpload.current?.(name);
            pendingUpload.current = null;
          }}
        />
      </main>

      <dialog ref={statsRef} onClick={(e) => e.target === statsRef.current && statsRef.current?.close()}>
        <h2>Your usage</h2>
        <div className="muted">Last 1,000 runs on this account</div>
        {stats && (
          <div className="stats-grid">
            <div className="stat"><b>{stats.runs}</b><span>runs</span></div>
            <div className="stat"><b>{stats.runs ? Math.round((stats.completed / stats.runs) * 100) : 0}%</b><span>completed</span></div>
            <div className="stat"><b>{stats.tool_calls}</b><span>tool calls</span></div>
            <div className="stat"><b>{stats.avg_latency_ms ? (stats.avg_latency_ms / 1000).toFixed(1) + "s" : "n/a"}</b><span>avg latency</span></div>
            <div className="stat"><b>{stats.p95_latency_ms ? (stats.p95_latency_ms / 1000).toFixed(1) + "s" : "n/a"}</b><span>p95 latency</span></div>
            <div className="stat"><b>${stats.total_cost_usd.toFixed(4)}</b><span>model spend</span></div>
          </div>
        )}
        <div style={{ textAlign: "right", marginTop: 14 }}><button className="btn" onClick={() => statsRef.current?.close()}>Close</button></div>
      </dialog>

      <dialog ref={mcpRef} onClick={(e) => e.target === mcpRef.current && mcpRef.current?.close()}>
        <h2>Use Atlas from any MCP client</h2>
        <div className="muted">
          Atlas is also an MCP server. Add it to Claude Desktop, Cursor, VS Code or your own agent and call <code>run_task</code>,{" "}
          <code>search_documents</code> or <code>list_documents</code> with your account&apos;s permissions and budget.
        </div>
        <div className="code-line">{mcpUrl}</div>
        <pre className="code-line" style={{ whiteSpace: "pre", display: "block" }}>
{`{
  "mcpServers": {
    "atlas": {
      "url": "${mcpUrl}",
      "headers": { "Authorization": "Bearer <your token>" }
    }
  }
}`}
        </pre>
        {info && (
          <div className="muted" style={{ marginTop: 12 }}>
            Model: {info.model}
            {info.fallbacks.length ? ` (fallbacks: ${info.fallbacks.join(", ")})` : ""} · Retrieval: {info.retrieval.mode}, rerank {info.retrieval.rerank} · Sandbox: {info.sandbox}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
          <button className="btn" onClick={() => { navigator.clipboard?.writeText(auth.token ?? ""); flash("Token copied. Keep it private."); }}>Copy my token</button>
          <button className="btn btn-primary" onClick={() => mcpRef.current?.close()}>Done</button>
        </div>
      </dialog>
    </div>
  );
}
