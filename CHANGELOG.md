# Changelog

## 1.1.0 (October 2026)

Version 1.1 turns Atlas from a working agent into a deployed, measured product: it adds MCP, real hybrid retrieval, analysis of uploaded data, a new interface, a public demo on Azure, and evaluation against a live model.

### Agent capabilities
- **MCP server.** `/mcp` (Streamable HTTP) exposes `run_task`, `search_documents` and `list_documents`. Calls are authenticated with the same JWT as the REST API, so an MCP client gets exactly the user's data, rate limit and budgets.
- **MCP client.** Tools from external MCP servers (`MCP_SERVERS`) are discovered, namespaced as `server__tool`, treated as untrusted content and skipped if the server is down.
- **Analysis of uploaded data.** `python_exec` mounts the user's CSV, XLSX, JSON and text uploads into the sandbox, so answers come from pandas on the real file, not from retrieved text. XLSX upload support was added.
- **Effort presets.** Fast, Balanced and Deep per run, controlling step, tool-call and self-review budgets.
- **Context engineering.** Older tool output is compacted when the transcript exceeds a character budget, and the user's file inventory goes into the system prompt.

### Retrieval
- Hybrid search replaces the fixed `0.7 * cosine + 0.3 * overlap` score: pgvector plus BM25 (Postgres full-text search with a GIN index), fused with Reciprocal Rank Fusion.
- Optional reranking with an LLM (listwise) or a hosted cross-encoder via LiteLLM. Failures fall back to the fused order.
- New retrieval eval: 44 labelled queries scored with recall@1/3/5, MRR and nDCG@5 per retriever, enforced in CI.

### Safety and cost
- Output guard masks API keys, tokens, private keys and JWTs in final answers.
- One-click guest accounts with a smaller daily budget, plus a global daily spend cap that protects a public demo's model key.
- Provider safety refusals (returned as API errors by some providers) become the answer instead of failing the run, with no retries or fallback.
- Tool calls a model writes as text (`<tool_use>...`) are recovered for known tools only.
- Prices for new OpenRouter models are fetched at startup, so cost tracking and budgets work for models LiteLLM doesn't know yet.

### Interface
- Rebuilt as a dark, monochrome, ChatGPT-style app using React Bits components (PromptBar, ThoughtLine, ShinyText, BlurText, SpotlightCard, Threads).
- Live "thinking" timeline, expandable tool cards with code and output, suggestion cards that run real tasks on sample data, `/commands`, `@file` mentions, voice input, chat search, a usage dialog and an MCP connect dialog.
- Sample Northwind Logistics data loadable in one click: sales data, 240 support tickets, a handbook, an API reference and an FAQ.

### Deployment
- `deploy/azure.sh`: one-command, repeatable deploy to Azure Container Apps (API, arq worker and Redis in one app; web app separately), Azure Database for PostgreSQL with pgvector, and images built in Azure Container Registry. It verifies the key, cleans pasted control characters, has a `SKIP_BUILD` fast path, and never leaves secrets on disk.
- `deploy/set-model.sh`: switch the live model in about a minute without rebuilding; it validates the model and its tool support first.
- Worker health check via `arq --check`.

### Quality
- Tests: 36 → 53, on SQLite and on Postgres + pgvector + Redis.
- Live eval on Inception Mercury-3: **96% (24/25)**, p50 9.9 s, median time to first token 0.71 s, $0.0015 per task.
- Eval fixes: the harness backs off on HTTP 429; the injection test now checks whether the attack was obeyed rather than quoted; the negative RAG test uses a policy absent from every document.
- CI: fixed an invalid workflow file that stopped CI from running at all, added the retrieval eval, and made nightly live evals opt-in.

## 1.0.0

The first release: LangGraph agent (plan, act, tools, reflect), web search, URL/PDF reading, sandboxed Python, document RAG, durable runs on Redis Streams, resumable SSE, LiteLLM routing with fallbacks, budgets, prompt-injection and SSRF defenses, Prometheus metrics, an eval harness and Docker deployment.
