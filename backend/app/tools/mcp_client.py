"""Atlas as an MCP client: tools from external MCP servers become agent tools.

Configure with MCP_SERVERS='[{"name": "docs", "url": "https://host/mcp", "headers": {"Authorization": "Bearer ..."}}]'.
Each remote tool is exposed as `<server>__<tool>`. Tool listings are cached for a few minutes; each call opens
a short-lived session, which keeps workers stateless. Remote output is treated as untrusted content and goes
through the same prompt-injection guard as web pages. A server that is down is skipped, never fatal.
"""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from typing import Any

from app.logging import log
from app.tools.base import Tool, ToolContext, ToolOutput

LIST_TTL_S = 300
_cache: dict[str, tuple[float, list[Tool]]] = {}


@asynccontextmanager
async def open_client(server: dict[str, Any]):
    """Connect to one configured server. Tests replace this with an in-process connection."""
    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with create_mcp_http_client(headers=server.get("headers") or {}) as http:
        async with Client(streamable_http_client(server["url"], http_client=http), read_timeout_seconds=60) as client:
            yield client


def _render(result: Any) -> tuple[str, bool]:
    parts = []
    for c in getattr(result, "content", None) or []:
        if getattr(c, "type", "") == "text":
            parts.append(c.text)
        else:
            parts.append(f"[{getattr(c, 'type', 'content')} omitted]")
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured and not parts:
        parts.append(str(structured))
    return "\n".join(parts) or "(no output)", bool(getattr(result, "is_error", None) or getattr(result, "isError", False))


def _wrap(server: dict[str, Any], spec: Any) -> Tool:
    prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", server["name"])[:20]
    remote = spec.name

    async def call(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        async with open_client(server) as client:
            result = await client.call_tool(remote, args)
        text, is_error = _render(result)
        return ToolOutput(text, untrusted=True, error=is_error)

    schema = getattr(spec, "input_schema", None) or getattr(spec, "inputSchema", None) or {"type": "object", "properties": {}}
    return Tool(
        name=f"{prefix}__{re.sub(r'[^a-zA-Z0-9_-]', '_', remote)}"[:64],
        description=f"[MCP:{server['name']}] {(spec.description or remote)[:900]}",
        parameters=schema,
        fn=call,
        timeout_s=float(server.get("timeout_s", 60)),
    )


async def load_mcp_tools(servers: list[dict[str, Any]]) -> list[Tool]:
    tools: list[Tool] = []
    for server in servers:
        if not server.get("name") or not server.get("url"):
            continue
        key = f"{server['name']}|{server['url']}"
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < LIST_TTL_S:
            tools.extend(cached[1])
            continue
        try:
            async with open_client(server) as client:
                listing = await client.list_tools()
            allowed = set(server.get("allow") or [])
            wrapped = [_wrap(server, t) for t in listing.tools if not allowed or t.name in allowed]
            _cache[key] = (time.monotonic(), wrapped)
            tools.extend(wrapped)
            log.info("mcp.tools_loaded", server=server["name"], count=len(wrapped))
        except Exception as e:  # noqa: BLE001 - an unreachable server must not break runs
            log.warning("mcp.unavailable", server=server["name"], error=str(e)[:200])
    return tools


def clear_cache() -> None:
    _cache.clear()
