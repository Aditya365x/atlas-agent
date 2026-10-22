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
- Query: two retrievers run on every search. Dense: pgvector HNSW (exact cosine elsewhere). Lexical: BM25, implemented with Postgres full-text search (`to_tsvector` + a GIN index, OR-ed query terms) in production and an in-process Okapi BM25 elsewhere. The two rankings are fused with **Reciprocal Rank Fusion** (`score = sum 1/(60 + rank)`).
- Rerank (optional): `RERANK=llm` asks the planner model for a listwise ranking of the fused top 20; `RERANK=cohere/rerank-v3.5` (or any LiteLLM rerank model) uses a hosted cross-encoder. Any reranker failure falls back to the fused order.
- Every query is filtered by `user_id`.

**Why RRF rather than a weighted score sum?** Cosine similarities and BM25 scores live on different, query-dependent scales, so a fixed `0.7/0.3` mix silently changes meaning from query to query. RRF only uses ranks, needs no calibration, and is what most production hybrid search uses.

**Measured, not assumed.** `evals/retrieval_eval.py` indexes the Northwind knowledge base and scores 44 labelled queries (paraphrases, keyword queries, exact error codes) per retriever with recall@1/3/5, MRR and nDCG@5. With the offline hashing embedder BM25 alone beats hybrid, because hashing "embeddings" are really lexical features; with a real embedding model the dense side adds the paraphrase recall BM25 misses. That result is why `EMBEDDING_MODEL` matters and why the retriever mode is a setting (`RETRIEVAL_MODE=hybrid|vector|lexical`) rather than hard-coded. CI fails if hybrid recall@5 drops below 0.85.

**Data files are also data.** Uploads of CSV/XLSX/JSON/TXT up to 10 MB keep their raw bytes. `python_exec` mounts the caller's files into the sandbox working directory, so the agent computes answers with pandas instead of guessing numbers from retrieved text chunks. The file list is part of the system prompt, so the model knows what it can load.

## 7. MCP: both directions

**Server.** `/mcp` is a Streamable HTTP MCP endpoint (official `mcp` SDK, stateless mode so any API replica can answer). It exposes `run_task`, `search_documents` and `list_documents`. A small ASGI wrapper verifies the same JWT as the REST API and binds the caller for the tool call, so an MCP client gets exactly the user's data, rate limit, daily quota and run budgets, nothing more. `run_task` goes through the same admission path (`start_run`) as `POST /v1/runs` and waits on the run's event stream.

**Client.** `MCP_SERVERS` lists external servers. Their tools are discovered at run start (listing cached for five minutes), namespaced as `server__tool`, and added to the registry next to the built-in tools. Remote output is untrusted content and goes through the prompt-injection guard. An unreachable server is logged and skipped; it never fails a run. `allow` limits which remote tools are exposed.

**Why both?** The server makes Atlas composable (another agent or an IDE can delegate whole tasks to it). The client means new capabilities (a CRM, a ticketing system, internal docs) are configuration, not code.

## 8. Context engineering, guardrails and cost control

- **Context budget.** Before each model call, if the transcript exceeds `MAX_CONTEXT_CHARS`, older tool outputs are cut to a 600-character head with a marker, keeping the system prompt, user turns and the four most recent tool results verbatim. Tool output is the bulkiest and least reused part of the context. A `context.compacted` event is emitted so the UI shows it happened.
- **Output guard.** The final answer is scanned for credential patterns (OpenAI/Anthropic/Google keys, GitHub tokens, AWS keys, private keys, JWTs) and they are masked before the answer is stored or returned. Input defense (section 5) stops instructions coming in; this stops secrets going out.
- **Effort presets.** Each run picks `fast` (3 steps, 4 tool calls, no self-review), `balanced` (defaults) or `deep` (14 steps, 24 tool calls, self-review). That makes the latency/cost/quality trade-off explicit per request.
- **Budgets for a public demo.** One-click guest accounts get a smaller daily allowance; a global daily cap (`GLOBAL_DAILY_COST_USD`) returns 503 for new runs once the whole deployment has spent it, which protects the model key behind a public link.

## 9. Data model

`users`, `conversations`, `messages` (with `run_id` and a JSON `meta` holding trace, images, sources, usage), `runs` (status, tokens, cost, latency, models used), `documents`, `chunks`. Runs and messages are separate so a failed or cancelled run leaves the conversation clean and still shows up in stats.

## 10. Failure handling

| Failure | Behaviour |
|---|---|
| Client disconnects | Run continues; client reconnects with `Last-Event-ID` |
| Provider down | Retries, then fallback model; if all fail the run ends `failed` with a clear event |
| Tool error or timeout | Becomes a tool message; the model can retry or work around it |
| Worker crash | Jobs are not auto-retried (tools may have side effects); startup marks orphaned runs `failed` so the UI never spins forever |
| Runaway loop | Step, tool and cost caps force a final answer |
| Postgres connection exhaustion | Bounded pool per process with a timeout; documented budget rule |

## 11. What would change at larger scale

- PgBouncer in transaction mode in front of Postgres.
- Move chunk storage to a dedicated vector store only if per-user corpora grow past what HNSW on Postgres serves comfortably.
- Pin resolved IPs in `fetch_url` to close the DNS-rebinding window.
- OpenTelemetry spans per node and tool call, exported alongside the Prometheus metrics.
- Per-tenant model routing and billing.
