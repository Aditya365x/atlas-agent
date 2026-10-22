"""Atlas as an MCP server (Streamable HTTP at /mcp).

Any MCP client (Claude Desktop/Code, Cursor, VS Code, another agent) can hand Atlas a task, search the
caller's documents, or list them. Calls are authenticated with the same JWT as the REST API, so every
tool is scoped to the caller and subject to the same rate limit, daily quota and run budgets.

    {"mcpServers": {"atlas": {"url": "https://<api>/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select

from app import db, rag
from app.config import get_settings
from app.infra import TERMINAL_EVENTS, get_infra
from app.models import Document, User
from app.security import decode_token

_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_user_id", default=None)


def _caller() -> str:
    uid = _user_id.get()
    if not uid:
        raise PermissionError("unauthenticated")
    return uid


def build_mcp() -> MCPServer:
    mcp = MCPServer(
        name="atlas",
        title="Atlas Agent",
        instructions=(
            "Atlas is an autonomous agent with web search, URL/PDF reading, a sandboxed Python interpreter and hybrid "
            "search over the caller's uploaded documents. Use run_task for anything multi-step."
        ),
        version="1.1.0",
    )

    @mcp.tool(
        description="Give Atlas a task. It plans, uses tools (web, Python, the caller's documents), self-reviews and returns the final answer with sources."
    )
    async def run_task(task: str, conversation_id: str | None = None) -> str:
        from app.runs import start_run

        uid = _caller()
        ok, retry = await get_infra().limiter.hit(f"runs:{uid}", get_settings().rate_limit_per_minute)
        if not ok:
            return f"Rate limit exceeded, retry in {retry}s."
        async with db.sessionmaker()() as session:
            user = await session.get(User, uid)
            if user is None:
                raise PermissionError("unknown user")
            try:
                created = await start_run(session, user, task, conversation_id)
            except HTTPException as e:
                return f"Could not start the run: {e.detail}"
        final: dict[str, Any] = {}
        tools: list[str] = []
        sources: list[str] = []
        async for _, ev in get_infra().bus.subscribe(created.run_id, None):
            if ev["type"] == "tool.call":
                tools.append(ev["tool"])
            elif ev["type"] == "tool.result":
                sources.extend(s["url"] for s in ev.get("sources") or [] if s.get("url") and s["url"] not in sources)
            if ev["type"] in TERMINAL_EVENTS:
                final = ev
                break
        if final.get("type") != "run.completed":
            return f"Run {final.get('type', 'ended')}: {final.get('error') or ''}".strip()
        usage = final.get("usage") or {}
        meta = {
            "run_id": created.run_id,
            "conversation_id": created.conversation_id,
            "tools_used": tools,
            "latency_ms": final.get("latency_ms"),
            "cost_usd": usage.get("cost_usd", 0),
        }
        tail = ("\n\nSources:\n" + "\n".join(f"- {u}" for u in sources[:10])) if sources and "Sources" not in final["answer"] else ""
        return f"{final['answer']}{tail}\n\n<!-- atlas {json.dumps(meta)} -->"

    @mcp.tool(description="Hybrid (vector + BM25, RRF-fused) search over the caller's uploaded documents. Returns passages with filenames.")
    async def search_documents(query: str, k: int = 5) -> str:
        uid = _caller()
        async with db.sessionmaker()() as session:
            hits = await rag.search(session, uid, query, k=max(1, min(int(k), 20)))
        if not hits:
            return "No matching passages. The caller may not have uploaded any documents."
        return "\n\n".join(f"[{h['filename']} #chunk{h['idx']} score={h['score']}]\n{h['text']}" for h in hits)

    @mcp.tool(description="List the caller's uploaded documents.")
    async def list_documents() -> str:
        uid = _caller()
        async with db.sessionmaker()() as session:
            rows = await session.execute(
                select(Document.filename, Document.num_chunks, Document.created_at)
                .where(Document.user_id == uid)
                .order_by(Document.created_at.desc())
            )
            docs = [{"filename": f, "chunks": n, "uploaded": str(c)} for f, n, c in rows]
        return json.dumps(docs) if docs else "No documents uploaded."

    return mcp


class MCPAuthApp:
    """ASGI wrapper: verifies the bearer JWT, binds the caller for the tools, then delegates to the MCP app."""

    def __init__(self, mcp: MCPServer) -> None:
        self.app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,  # any API replica can serve any request
            json_response=True,
            # Host/Origin checks are for local servers reachable from a browser; this one sits behind auth + CORS.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        try:
            if not auth.lower().startswith("bearer "):
                raise HTTPException(401, "Missing bearer token")
            uid = decode_token(auth[7:].strip())
        except HTTPException as e:
            body = json.dumps({"detail": e.detail}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = _user_id.set(uid)
        try:
            await self.app({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)
        finally:
            _user_id.reset(token)
