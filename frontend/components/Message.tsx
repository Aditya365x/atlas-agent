"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentEvent, Image, Source, Usage } from "@/lib/api";
import ThoughtLine from "./bits/ThoughtLine";
import ToolCards, { liveLabel, toSteps } from "./Route";

export type AssistantView = {
  content: string;
  events: AgentEvent[];
  images: Image[];
  sources: Source[];
  usage?: Usage;
  latencyMs?: number;
  live: boolean;
  error?: string;
  cancelled?: boolean;
};

function hostOf(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function UserMessage({ text }: { text: string }) {
  return (
    <div className="msg-user">
      <div>{text}</div>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn btn-ghost icon-btn"
      aria-label={done ? "Copied" : "Copy answer"}
      title={done ? "Copied" : "Copy"}
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        });
      }}
    >
      {done ? (
        <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden><rect x="5" y="5" width="8.5" height="8.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M10.5 5V3.5a1.5 1.5 0 00-1.5-1.5H4A1.5 1.5 0 002.5 3.5V9A1.5 1.5 0 004 10.5h1" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
      )}
    </button>
  );
}

export function AssistantMessage({ view, onRetry }: { view: AssistantView; onRetry?: () => void }) {
  const { content, events, images, sources, usage, latencyMs, live, error, cancelled } = view;
  const steps = toSteps(events, live);
  const hasWork = events.some((e) => e.type === "tool.call" || e.type === "reflection");
  const webSources = sources.filter((s) => s.url);
  const tokens = (usage?.prompt_tokens ?? 0) + (usage?.completion_tokens ?? 0);

  return (
    <div className="msg-assistant">
      {(live || hasWork) && (
        <div className="thought-wrap">
          <ThoughtLine
            label={liveLabel(events)}
            doneLabel={hasWork ? "Worked for" : "Thought for"}
            working={live && !content}
            steps={steps}
            elapsed={!live && latencyMs !== undefined ? latencyMs / 1000 : undefined}
            glyph="dot"
            color="#8a8a8a"
            glyphColor="#ffffff"
            fontSize={14}
            collapseOnSettle
          />
        </div>
      )}
      {hasWork && <ToolCards events={events} live={live} />}

      {content && (
        <div className={`answer ${live ? "streaming" : ""}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: (p) => <a {...p} target="_blank" rel="noopener noreferrer" /> }}>
            {content}
          </ReactMarkdown>
        </div>
      )}

      {images.length > 0 && (
        <div className="figures">
          {images.map((im, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={i} src={`data:${im.mime};base64,${im.b64}`} alt={im.name || `Chart ${i + 1} produced by Atlas`} />
          ))}
        </div>
      )}

      {webSources.length > 0 && !live && (
        <div className="sources">
          {webSources.slice(0, 8).map((s, i) => (
            <a key={s.url} className="source" href={s.url} target="_blank" rel="noopener noreferrer" title={s.title}>
              <b>{i + 1}</b>
              <span>{s.title || hostOf(s.url)}</span>
            </a>
          ))}
        </div>
      )}

      {error && (
        <div className="error-box" role="alert">
          {error} {onRetry && <button className="btn btn-ghost" style={{ padding: "2px 8px", marginLeft: 6 }} onClick={onRetry}>Retry</button>}
        </div>
      )}
      {cancelled && <div className="notice">Stopped.</div>}

      {!live && content && (
        <div className="actions">
          <CopyButton text={content} />
          {usage && latencyMs !== undefined && (
            <span className="meta">
              <span title="End-to-end latency">{(latencyMs / 1000).toFixed(1)}s</span>
              {usage.ttft_s !== undefined && <span title="Time to first token">TTFT {usage.ttft_s.toFixed(2)}s</span>}
              <span>{tokens.toLocaleString()} tok</span>
              <span>${(usage.cost_usd ?? 0).toFixed(4)}</span>
              {usage.models?.length ? <span>{usage.models.join(", ")}</span> : null}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
