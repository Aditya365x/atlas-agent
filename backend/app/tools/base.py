"""Tool abstraction: JSON-schema'd async functions with timeouts, metrics and error isolation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.guard import wrap_untrusted
from app.logging import log
from app.metrics import TOOL_CALLS


@dataclass
class ToolContext:
    user_id: str
    run_id: str


@dataclass
class ToolOutput:
    text: str
    images: list[dict[str, str]] = field(default_factory=list)  # [{"mime": "image/png", "b64": ...}]
    sources: list[dict[str, str]] = field(default_factory=list)  # [{"title":..., "url":...}]
    untrusted: bool = False  # external content -> wrapped + scanned for prompt injection
    error: bool = False


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutput]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn
    timeout_s: float = 45.0

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[ToolOutput, str, list[str]]:
        """Run a tool. Returns (output, text_for_model, guard_flags). Never raises."""
        tool = self._tools.get(name)
        if tool is None:
            TOOL_CALLS.labels(name, "unknown").inc()
            return ToolOutput(f"Error: unknown tool '{name}'. Available: {self.names()}", error=True), "", []
        start = time.perf_counter()
        try:
            out = await asyncio.wait_for(tool.fn(args, ctx), timeout=tool.timeout_s)
            TOOL_CALLS.labels(name, "error" if out.error else "ok").inc()
        except TimeoutError:
            TOOL_CALLS.labels(name, "timeout").inc()
            out = ToolOutput(f"Error: tool '{name}' timed out after {tool.timeout_s}s", error=True)
        except Exception as e:  # noqa: BLE001 - tool failures must never crash the run
            TOOL_CALLS.labels(name, "error").inc()
            log.exception("tool.error", tool=name)
            out = ToolOutput(f"Error: {type(e).__name__}: {str(e)[:500]}", error=True)
        log.info("tool.done", tool=name, ms=int((time.perf_counter() - start) * 1000), error=out.error)
        flags: list[str] = []
        text = out.text
        if out.untrusted and not out.error:
            text, flags = wrap_untrusted(name, out.text)
        return out, text, flags
