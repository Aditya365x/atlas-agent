"use client";

import { useState } from "react";
import type { AgentEvent } from "@/lib/api";

type Waypoint = {
  key: string;
  state: "done" | "active" | "fail" | "warn";
  title: React.ReactNode;
  meta?: string;
  body?: React.ReactNode;
};

const TOOL_LABEL: Record<string, string> = {
  web_search: "Searched the web",
  fetch_url: "Read a page",
  python_exec: "Ran Python",
  file_search: "Searched your files",
};

function argsSummary(tool: string, args: Record<string, unknown>): string {
  if (tool === "web_search" || tool === "file_search") return `“${String(args.query ?? "")}”`;
  if (tool === "fetch_url") return String(args.url ?? "");
  return "";
}

/** Turns the raw event stream into route waypoints. Works for live runs and for saved traces. */
export function toWaypoints(events: AgentEvent[], live: boolean): Waypoint[] {
  const wps: Waypoint[] = [];
  const results = new Map<string, Extract<AgentEvent, { type: "tool.result" }>>();
  for (const e of events) if (e.type === "tool.result") results.set(e.id, e);

  for (const e of events) {
    if (e.type === "plan") {
      wps.push({
        key: "plan",
        state: "done",
        title: e.needs_tools ? "Planned the work" : "Answering directly",
        body: e.needs_tools && e.steps.length > 1 ? <ol>{e.steps.map((s, i) => <li key={i}>{s}</li>)}</ol> : undefined,
      });
    } else if (e.type === "answer.reset" && e.note) {
      wps.push({ key: `note-${wps.length}`, state: "done", title: "Thinking", body: <span>{e.note}</span> });
    } else if (e.type === "tool.call") {
      const r = results.get(e.id);
      const summary = argsSummary(e.tool, e.args);
      wps.push({
        key: e.id,
        state: !r ? (live ? "active" : "fail") : r.ok ? "done" : "fail",
        title: (
          <>
            {TOOL_LABEL[e.tool] ?? "Used a tool"} <span className="tool-name">{e.tool}</span>
          </>
        ),
        meta: r ? `${(r.ms / 1000).toFixed(1)}s${r.ok ? "" : ", failed"}` : live ? "running" : undefined,
        body: (
          <>
            {summary && <div>{summary}</div>}
            <details>
              <summary>{e.tool === "python_exec" ? "Code and output" : "Details"}</summary>
              {e.tool === "python_exec" ? <pre>{String(e.args.code ?? "")}</pre> : <pre>{JSON.stringify(e.args, null, 2)}</pre>}
              {r && <pre>{r.preview || "(no output)"}</pre>}
            </details>
          </>
        ),
      });
    } else if (e.type === "guard.flag") {
      wps.push({
        key: `guard-${wps.length}`,
        state: "warn",
        title: <span className="flag">Ignored suspicious instructions in a source</span>,
        body: <span>Patterns found: {e.flags.join(", ")}. Content was treated as data only.</span>,
      });
    } else if (e.type === "reflection") {
      wps.push({
        key: `reflect-${wps.length}`,
        state: e.revise ? "warn" : "done",
        title: e.revise ? "Self-review found issues, revising" : "Self-review passed",
        meta: `${e.score}/10`,
        body: e.feedback ? <span>{e.feedback}</span> : undefined,
      });
    }
  }
  return wps;
}

export default function Route({ events, live, defaultOpen }: { events: AgentEvent[]; live: boolean; defaultOpen?: boolean }) {
  const [open, setOpen] = useState<boolean | null>(defaultOpen ?? null);
  const wps = toWaypoints(events, live);
  if (!wps.length) return null;
  const expanded = open ?? live;
  const tools = events.filter((e) => e.type === "tool.call").length;
  const label = live ? "Working" : tools ? `Route: ${tools} tool ${tools === 1 ? "call" : "calls"}` : "Route";

  return (
    <div className="route">
      <button className="route-toggle" aria-expanded={expanded} onClick={() => setOpen(!expanded)}>
        <svg className="chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden>
          <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        {label}
      </button>
      {expanded && (
        <ol className="waypoints">
          {wps.map((w) => (
            <li key={w.key} className={`wp ${w.state}`}>
              <span className="wp-dot" aria-hidden />
              <div className="wp-title">
                {w.title}
                {w.meta && <small>{w.meta}</small>}
              </div>
              {w.body && <div className="wp-body">{w.body}</div>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
