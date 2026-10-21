"""Prometheus metrics exposed at /metrics."""

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter("atlas_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("atlas_http_request_seconds", "HTTP latency", ["route"])
RUNS = Counter("atlas_runs_total", "Agent runs by terminal status", ["status"])
RUN_LATENCY = Histogram("atlas_run_seconds", "End-to-end agent run latency", buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300))
TTFT = Histogram("atlas_time_to_first_token_seconds", "Time to first streamed token", buckets=(0.25, 0.5, 1, 2, 4, 8))
LLM_CALLS = Counter("atlas_llm_calls_total", "LLM calls", ["model", "status"])
LLM_COST = Counter("atlas_llm_cost_usd_total", "LLM spend in USD", ["model"])
LLM_TOKENS = Counter("atlas_llm_tokens_total", "LLM tokens", ["model", "kind"])
TOOL_CALLS = Counter("atlas_tool_calls_total", "Tool invocations", ["tool", "status"])
GUARD_FLAGS = Counter("atlas_guard_flags_total", "Prompt-injection heuristics triggered", ["source"])
