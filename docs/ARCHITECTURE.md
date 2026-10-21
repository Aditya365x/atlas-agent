# Architecture and design decisions

This document explains why Atlas is built the way it is. Each section names the decision, the alternative that was rejected, and the trade-off accepted.

## 1. Request path

1. `POST /v1/runs` authenticates, applies the per-user rate limit and daily spend quota, writes the user message and a `queued` run in one transaction, enqueues the run and returns `202` with a run id.
2. A worker picks the job, flips the run to `running` (jobs for runs not in `queued` are skipped, which makes duplicate deliveries harmless) and executes the agent graph.
3. Every event the graph emits is appended to the Redis Stream `run:{id}:events`.
4. `GET /v1/runs/{id}/events` replays the stream from the start, or from `Last-Event-ID`, then blocks on `XREAD` for new entries and closes after the terminal event.
5. On completion the worker stores the assistant message with its trace, images, sources and usage, and finalises the run row with tokens, cost and latency.

**Why not stream straight from the request handler?** Agent runs last from seconds to minutes. Tying them to an HTTP connection means a mobile network blip, a closed tab or an API deploy kills the work and its cost is wasted. Decoupling execution from delivery also lets API and workers scale independently.

**Why Redis Streams rather than pub/sub?** Pub/sub drops messages for subscribers that are not connected at that instant. Streams keep an ordered, replayable log with ids that map directly onto SSE's `Last-Event-ID`. Streams are capped (`MAXLEN ~20000`) and expire after 24 hours; after that the endpoint synthesises the terminal event from Postgres.

**Why SSE rather than WebSockets?** The flow is one-directional (server to client). SSE works over plain HTTP, through most proxies, has resume semantics built in, and needs no connection state on the server beyond the stream cursor. Control actions (cancel) are ordinary POSTs.

## 2. The agent graph

LangGraph is used as an explicit state machine (`plan -> agent <-> tools -> reflect -> finalize`), not as a prebuilt ReAct agent, so every transition is visible, testable and budgeted.

- **Planner.** Returns `{needs_tools, steps}`. When no tools are needed the main call is made without tool schemas, which removes several hundred prompt tokens per call and skips reflection. A wrong "no tools" decision is the cost; the planner prompt biases towards tools for anything factual or numeric.
- **Parallel tool calls.** All calls in one model turn run concurrently with `asyncio.gather`; each has its own timeout and can never raise into the graph (failures become tool messages the model can react to).
- **Reflection.** A critic sees the request, the evidence and the draft and returns a score. Below 7 with concrete feedback, the agent revises once. One revision captures most of the gain; unbounded loops are expensive and can oscillate.
- **Budgets.** Step count, tool-call count and run cost are checked between steps. Hitting one sets `force_answer`, which makes the next call tool-free with an instruction to answer from the evidence so far. The run always ends with an answer, never a silent stop.
- **Cancellation.** A Redis flag checked before each model call. Cooperative cancellation keeps state consistent; a model call already in flight is allowed to finish.

## 3. Model layer

`LiteLLMRouter` wraps LiteLLM's async streaming API:

- Streams deltas to a callback (tokens go to the UI immediately) and rebuilds the full response, including streamed tool calls and usage, with `stream_chunk_builder`.
- Classifies errors. Rate limits, timeouts, connection errors and 5xx are retried with jittered exponential backoff. Auth errors and bad requests skip straight to the next model in `MODEL_FALLBACKS`.
- Records prompt and completion tokens and USD cost per call (Prometheus counters by model).

`FakeLLM` implements the same interface deterministically. It powers the unit tests (scripted mode), the offline eval suite in CI, keyless demos and load tests that isolate platform overhead from model latency.

## 4. Tools and isolation

**python_exec** has three backends behind one interface:

| Backend | Isolation | Use |
|---|---|---|
| `local` | Separate process, rlimits (CPU, 1.5 GB address space, 25 MB files, 256 fds, no core dumps), process-group kill on timeout, scrubbed env, temp dir | Development and tests |
| `http` | The above, inside a container that is non-root, read-only except `/tmp`, `cap_drop: ALL`, `no-new-privileges`, pids and memory limits, on an internal network with no route to the internet or to Postgres/Redis, token-authenticated | Docker / self-hosted |
| `e2b` | Firecracker microVM per execution | Managed production |

Images the code saves are collected and returned as base64, shown in the UI and kept with the message.

**fetch_url** resolves the host for every hop and rejects anything that is not globally routable (loopback, RFC 1918, link-local including `169.254.169.254`, multicast). Redirects are followed manually so each target is checked. Bodies are capped at 3 MB. Known gap: DNS rebinding between the check and the connection; the fix is to pin the resolved IP in a custom transport.

## 5. Prompt-injection defense

Content from tools that touch the outside world is marked `untrusted` and wrapped:

```
<untrusted_content source="fetch_url"> ... </untrusted_content>
```

The system prompt states that this content is data and must not be followed. Any `untrusted_content` tag inside the payload is replaced, so a page cannot close the wrapper and inject text that appears trusted. Regex heuristics flag common attack phrasing; a flag appends a warning for the model, emits a `guard.flag` event to the UI and increments a metric. Heuristics are a signal, not a guarantee; the structural separation and the least-privilege tools (no tool can send data to an arbitrary destination) do the heavy lifting.

## 6. Retrieval

- Extraction: pypdf for PDFs (with page markers), HTML to text, plain text otherwise.
- Chunking: paragraph-aware, 1200 characters with 200 overlap, hard split for giant paragraphs.
- Embeddings: any LiteLLM embedding model, truncated to 384 dimensions (Matryoshka-style) and renormalised, or a local feature-hashing embedder that needs no network.
- Storage: chunks keep a portable JSON copy of the vector; on Postgres the `vector(384)` column and an HNSW cosine index are added at startup.
- Query: ANN pre-selects 40 candidates, then hybrid re-ranking `0.7 * cosine + 0.3 * lexical overlap`. The lexical term rescues exact strings (error codes, names, numbers) that embeddings blur.
- Every query is filtered by `user_id`.

## 7. Data model

`users`, `conversations`, `messages` (with `run_id` and a JSON `meta` holding trace, images, sources, usage), `runs` (status, tokens, cost, latency, models used), `documents`, `chunks`. Runs and messages are separate so a failed or cancelled run leaves the conversation clean and still shows up in stats.

## 8. Failure handling

| Failure | Behaviour |
|---|---|
| Client disconnects | Run continues; client reconnects with `Last-Event-ID` |
| Provider down | Retries, then fallback model; if all fail the run ends `failed` with a clear event |
| Tool error or timeout | Becomes a tool message; the model can retry or work around it |
| Worker crash | Jobs are not auto-retried (tools may have side effects); startup marks orphaned runs `failed` so the UI never spins forever |
| Runaway loop | Step, tool and cost caps force a final answer |
| Postgres connection exhaustion | Bounded pool per process with a timeout; documented budget rule |

## 9. What would change at larger scale

- PgBouncer in transaction mode in front of Postgres.
- Move chunk storage to a dedicated vector store only if per-user corpora grow past what HNSW on Postgres serves comfortably.
- Pin resolved IPs in `fetch_url` to close the DNS-rebinding window.
- OpenTelemetry spans per node and tool call, exported alongside the Prometheus metrics.
- Per-tenant model routing and billing.
