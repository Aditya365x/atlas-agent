"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentEvent, Image, Source, Usage } from "@/lib/api";
import Route from "./Route";

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
  return <div className="msg-user">{text}</div>;
}

export function AssistantMessage({ view }: { view: AssistantView }) {
  const { content, events, images, sources, usage, latencyMs, live, error, cancelled } = view;
  const waiting = live && !content;
  return (
    <div className="msg-assistant">
      <Route events={events} live={live} />
      {content && (
        <div className={`answer ${live ? "cursor" : ""}`}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{ a: (p) => <a {...p} target="_blank" rel="noopener noreferrer" /> }}
          >
            {content}
          </ReactMarkdown>
        </div>
      )}
      {waiting && !events.length && <div className="answer cursor" aria-label="Atlas is starting" />}
      {images.length > 0 && (
        <div className="figures">
          {images.map((im, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={i} src={`data:${im.mime};base64,${im.b64}`} alt={im.name || `Chart ${i + 1} produced by Atlas`} />
          ))}
        </div>
      )}
      {sources.filter((s) => s.url).length > 0 && !live && (
        <div className="sources">
          {sources
            .filter((s) => s.url)
            .slice(0, 8)
            .map((s, i) => (
              <a key={s.url} className="source" href={s.url} target="_blank" rel="noopener noreferrer" title={s.title}>
                <b>{i + 1}</b>
                <span>{s.title || hostOf(s.url)}</span>
              </a>
            ))}
        </div>
      )}
      {error && <div className="error-box" role="alert">{error}</div>}
      {cancelled && <div className="run-meta">Stopped.</div>}
      {!live && usage && latencyMs !== undefined && (
        <div className="run-meta">
          <span>{(latencyMs / 1000).toFixed(1)}s</span>
          {usage.ttft_s !== undefined && <span>first token {usage.ttft_s.toFixed(2)}s</span>}
          <span>{((usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0)).toLocaleString()} tokens</span>
          <span>${(usage.cost_usd ?? 0).toFixed(4)}</span>
          {usage.models?.length ? <span>{usage.models.join(", ")}</span> : null}
        </div>
      )}
    </div>
  );
}
