"""Provider-agnostic LLM layer.

`LiteLLMRouter` gives one interface over 100+ providers with:
  * token streaming (callback per delta) incl. streamed tool calls
  * retries with exponential backoff on transient errors (429/5xx/timeouts)
  * ordered model fallbacks (e.g. Gemini -> DeepSeek -> GPT) when a provider is down
  * per-call token + USD cost accounting and Prometheus metrics

`FakeLLM` is a deterministic offline model used by tests, CI evals and keyless demos.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import Settings, get_settings
from app.logging import log
from app.metrics import LLM_CALLS, LLM_COST, LLM_TOKENS

Role = Literal["main", "planner", "critic"]
TokenCallback = Callable[[str], Awaitable[None]]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as returned by the model

    def parsed_args(self) -> dict[str, Any]:
        try:
            val = json.loads(self.arguments or "{}")
            return val if isinstance(val, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class LLMResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class LLM:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        role: Role = "main",
        tools: list[dict[str, Any]] | None = None,
        on_token: TokenCallback | None = None,
    ) -> LLMResult:
        raise NotImplementedError


class LLMUnavailable(RuntimeError):
    pass


class LiteLLMRouter(LLM):
    def __init__(self, settings: Settings | None = None) -> None:
        import litellm

        self.s = settings or get_settings()
        self.litellm = litellm
        litellm.drop_params = True  # tolerate provider-specific param differences
        litellm.suppress_debug_info = True
        if self.s.langfuse_public_key and self.s.langfuse_secret_key:
            litellm.success_callback = ["langfuse"]
            litellm.failure_callback = ["langfuse"]

    def _models_for(self, role: Role) -> list[str]:
        primary = self.s.planner_model if role in ("planner", "critic") else self.s.model_main
        return [primary, *[m for m in self.s.model_fallbacks if m != primary]]

    def _is_retryable(self, e: Exception) -> bool:
        lt = self.litellm
        retryable = tuple(
            getattr(lt, n)
            for n in ("RateLimitError", "APIConnectionError", "Timeout", "ServiceUnavailableError", "InternalServerError")
            if hasattr(lt, n)
        )
        return isinstance(e, retryable + (asyncio.TimeoutError, TimeoutError))

    async def complete(self, messages, *, role="main", tools=None, on_token=None) -> LLMResult:
        last_err: Exception | None = None
        for model in self._models_for(role):
            for attempt in range(self.s.llm_max_retries + 1):
                try:
                    result = await self._stream_once(model, messages, tools, on_token)
                    LLM_CALLS.labels(model, "ok").inc()
                    return result
                except Exception as e:  # noqa: BLE001 - classify below
                    last_err = e
                    LLM_CALLS.labels(model, "error").inc()
                    retry = self._is_retryable(e) and attempt < self.s.llm_max_retries
                    log.warning("llm.error", model=model, attempt=attempt, retry=retry, error=str(e)[:300])
                    if not retry:
                        break  # non-retryable (auth, bad request) -> next model
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.random() * 0.25)
        raise LLMUnavailable(f"All models failed. Last error: {last_err}")

    async def _stream_once(self, model, messages, tools, on_token) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": self.s.llm_timeout_s,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        stream = await self.litellm.acompletion(**kwargs)
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
            if on_token and chunk.choices:
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    await on_token(delta.content)
        resp = self.litellm.stream_chunk_builder(chunks, messages=messages)
        msg = resp.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:8]}", name=tc.function.name, arguments=tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        try:
            cost = float(self.litellm.completion_cost(completion_response=resp, model=model) or 0.0)
        except Exception:  # unknown pricing for this model
            cost = 0.0
        LLM_TOKENS.labels(model, "prompt").inc(pt)
        LLM_TOKENS.labels(model, "completion").inc(ct)
        LLM_COST.labels(model).inc(cost)
        return LLMResult(msg.content or "", tool_calls, model, pt, ct, cost)


# --------------------------------------------------------------------------------------
# Deterministic fake model
# --------------------------------------------------------------------------------------

_MATH = re.compile(r"(\(*-?\d[\d\s\.\*\+\-/\(\)%]*[\*\+\-/%][\d\s\.\*\+\-/\(\)%]*\d\)?)")


class FakeLLM(LLM):
    """Rule-based stand-in. Pass `script` to return exact results in order (unit tests)."""

    def __init__(self, script: list[LLMResult] | None = None, token_delay: float = 0.0) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self.token_delay = token_delay

    async def complete(self, messages, *, role="main", tools=None, on_token=None) -> LLMResult:
        self.calls.append({"role": role, "messages": messages, "tools": [t["function"]["name"] for t in tools or []]})
        result = self.script.pop(0) if self.script else self._respond(messages, role, tools)
        result.model = result.model or "fake/atlas-1"
        result.prompt_tokens = result.prompt_tokens or sum(len(str(m.get("content", ""))) // 4 for m in messages)
        result.completion_tokens = result.completion_tokens or max(1, len(result.content) // 4)
        if on_token and result.content:
            for tok in re.findall(r"\S+\s*", result.content):
                await on_token(tok)
                if self.token_delay:
                    await asyncio.sleep(self.token_delay)
        return result

    @staticmethod
    def _user_query(messages) -> str:
        for m in reversed(messages):
            if m["role"] == "user":
                return str(m["content"])
        return ""

    def _respond(self, messages, role, tools) -> LLMResult:
        q = self._user_query(messages)
        ql = q.lower()
        if role == "planner":
            needs = bool(re.search(r"search|latest|news|today|plot|chart|calculat|compute|file|document|http", ql)) or bool(_MATH.search(q))
            steps = ["Understand the request", "Gather facts with tools", "Write the answer"] if needs else ["Answer"]
            return LLMResult(json.dumps({"needs_tools": needs, "steps": steps}))
        if role == "critic":
            return LLMResult(json.dumps({"score": 9, "feedback": ""}))

        last = messages[-1]
        names = {t["function"]["name"] for t in tools or []}
        if last["role"] == "tool":
            observations = [m["content"] for m in messages if m["role"] == "tool"]
            summary = " ".join(o[:300] for o in observations)
            return LLMResult(f"Here is what I found.\n\n{_strip_untrusted(summary)}\n\nLet me know if you want more detail.")

        def call(name: str, args: dict) -> LLMResult:
            return LLMResult(tool_calls=[ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, json.dumps(args))])

        if "python_exec" in names and re.search(r"plot|chart|graph", ql):
            code = (
                "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
                "xs=list(range(1,11)); ys=[x*x for x in xs]\nplt.plot(xs, ys, marker='o'); plt.title('y = x^2')\n"
                "plt.savefig('chart.png', dpi=110)\nprint('saved chart.png; max y =', max(ys))"
            )
            return call("python_exec", {"code": code})
        m = _MATH.search(q)
        if "python_exec" in names and (m or re.search(r"calculat|compute", ql)):
            expr = m.group(1).strip() if m else "2+2"
            return call("python_exec", {"code": f"print({expr})"})
        if "file_search" in names and re.search(r"my (file|doc)|uploaded|document", ql):
            return call("file_search", {"query": q})
        url = re.search(r"https?://\S+", q)
        if "fetch_url" in names and url:
            return call("fetch_url", {"url": url.group(0)})
        if "web_search" in names and re.search(r"search|latest|news|today", ql):
            return call("web_search", {"query": q[:200]})
        return LLMResult(f"(offline demo model) You asked: {q.strip()[:500]}")


def _strip_untrusted(text: str) -> str:
    return re.sub(r"</?untrusted_content[^>]*>", "", text).strip()


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = FakeLLM(token_delay=0.01) if get_settings().llm_mode == "fake" else LiteLLMRouter()
    return _llm


def set_llm(llm: LLM | None) -> None:
    global _llm
    _llm = llm
