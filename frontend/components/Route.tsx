"use client";

import type { AgentEvent } from "@/lib/api";

const TOOL: Record<string, { live: string; done: string; icon: string }> = {
  web_search: { live: "Searching the web", done: "Searched the web", icon: "⌕" },
  fetch_url: { live: "Reading a page", done: "Read a page", icon: "↗" },
  python_exec: { live: "Running Python", done: "Ran Python", icon: "›_" },
  file_search: { live: "Searching your files", done: "Searched your files", icon: "▤" },
};

function toolInfo(name: string) {
  if (TOOL[name]) return TOOL[name];
  const [server, tool] = name.includes("__") ? name.split("__") : ["tool", name];
  return { live: `Calling ${tool} on ${server}`, done: `Called ${tool} on ${server} (MCP)`, icon: "◇" };
}

function argsSummary(tool: string, args: Record<string, unknown>): string {
  if (tool === "web_search" || tool === "file_search") return `“${String(args.query ?? "")}”`;
  if (tool === "fetch_url") return String(args.url ?? "");
  return "";
}

/** One-line steps for the ThoughtLine trace. */
export function toSteps(events: AgentEvent[], live: boolean): string[] {
  const steps: string[] = [];
  const done = new Set(events.filter((e) => e.type === "tool.result").map((e) => (e as { id: string }).id));
  for (const e of events) {
    if (e.type === "plan") steps.push(e.needs_tools ? `Planned: ${e.steps.join(" → ")}` : "Answering directly");
    else if (e.type === "tool.call") {
      const t = toolInfo(e.tool);
      const s = argsSummary(e.tool, e.args);
      steps.push(`${done.has(e.id) || !live ? t.done : t.live}${s ? ` ${s}` : ""}`);
    } else if (e.type === "guard.flag") steps.push(e.tool === "output" ? "Redacted a credential from the answer" : "Ignored injected instructions in a source");
    else if (e.type === "reflection") steps.push(e.revise ? `Self-review ${e.score}/10, revising` : `Self-review passed ${e.score}/10`);
    else if (e.type === "context.compacted") steps.push(`Compacted older context (${Math.round(e.chars_saved / 1000)}k chars)`);
  }
  if (live) steps.push("Writing the answer");
  return steps;
}

export function liveLabel(events: AgentEvent[]): string {
  const results = new Set(events.filter((e) => e.type === "tool.result").map((e) => (e as { id: string }).id));
  const pending = [...events].reverse().find((e) => e.type === "tool.call" && !results.has(e.id)) as
    | Extract<AgentEvent, { type: "tool.call" }>
    | undefined;
  if (pending) return `${toolInfo(pending.tool).live}…`;
  const last = events[events.length - 1];
  if (!last) return "Thinking…";
  if (last.type === "reflection") return "Reviewing its answer…";
  if (last.type === "plan") return last.needs_tools ? "Planning…" : "Thinking…";
  return "Thinking…";
}

/** Expandable cards with the exact inputs and outputs of every tool call. */
export default function ToolCards({ events, live }: { events: AgentEvent[]; live: boolean }) {
  const results = new Map<string, Extract<AgentEvent, { type: "tool.result" }>>();
  for (const e of events) if (e.type === "tool.result") results.set(e.id, e);
  const cards = events.filter((e) => e.type === "tool.call" || (e.type === "guard.flag" && e.tool !== "output") || (e.type === "reflection" && e.feedback));
  if (!cards.length) return null;
  return (
    <div className="tools">
      {cards.map((e, i) => {
        if (e.type === "guard.flag")
          return (
            <details key={`g${i}`} className="tool" data-state="warn">
              <summary>
                <span className="t-icon">!</span>
                <span className="t-title">Prompt-injection attempt ignored <code>{e.tool}</code></span>
              </summary>
              <div className="t-body">Patterns found: {e.flags.join(", ")}. The content was treated strictly as data.</div>
            </details>
          );
        if (e.type === "reflection")
          return (
            <details key={`r${i}`} className="tool" data-state={e.revise ? "warn" : "done"}>
              <summary>
                <span className="t-icon">✓</span>
                <span className="t-title">Self-review</span>
                <span className="t-meta">{e.score}/10</span>
              </summary>
              <div className="t-body">{e.feedback}</div>
            </details>
          );
        if (e.type !== "tool.call") return null;
        const r = results.get(e.id);
        const t = toolInfo(e.tool);
        const state = !r ? (live ? "active" : "fail") : r.ok ? "done" : "fail";
        const summary = argsSummary(e.tool, e.args);
        return (
          <details key={e.id} className="tool" data-state={state}>
            <summary>
              <span className="t-icon">{state === "active" ? <span className="spinner" /> : t.icon}</span>
              <span className="t-title">
                {r || !live ? t.done : t.live}
                {summary ? <code>{summary}</code> : <code>{e.tool}</code>}
              </span>
              <span className="t-meta">{r ? `${(r.ms / 1000).toFixed(1)}s${r.ok ? "" : " · failed"}` : live ? "running" : ""}</span>
            </summary>
            <div className="t-body">
              <div className="lbl">{e.tool === "python_exec" ? "Code" : "Input"}</div>
              <pre>{e.tool === "python_exec" ? String(e.args.code ?? "") : JSON.stringify(e.args, null, 2)}</pre>
              {r && (
                <>
                  <div className="lbl">Output</div>
                  <pre>{r.preview || "(no output)"}</pre>
                </>
              )}
            </div>
          </details>
        );
      })}
    </div>
  );
}
