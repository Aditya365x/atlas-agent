"""Hybrid retrieval, context compaction, output guard, data files in the sandbox, guest/demo mode, MCP server + client."""

import json

import pytest

from app import db, rag
from app.agent.graph import compact_messages
from app.agent.guard import redact_secrets
from app.config import get_settings
from app.sandbox_runner import run_python
from app.tools import ToolContext, default_registry
from tests.conftest import signup
from tests.test_api import stream_events

# ---------------- retrieval ----------------


def test_bm25_prefers_rare_exact_terms():
    docs = ["error E-4031 means a driver is assigned", "the shipment was delivered", "drivers deliver shipments daily"]
    scores = rag.bm25_scores("what is E-4031", docs)
    assert scores.index(max(scores)) == 0
    assert rag.bm25_scores("the of and", docs) == [0.0, 0.0, 0.0]  # stopword-only queries score nothing


def test_rrf_rewards_agreement_between_rankers():
    fused = rag.rrf(["a", "b", "c"], ["b", "c", "a"])
    assert max(fused, key=fused.get) == "b"
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 63)


async def _user(user_id: str) -> str:
    from app.models import User

    async with db.sessionmaker()() as s:
        if await s.get(User, user_id) is None:
            s.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x"))
            await s.commit()
    return user_id


async def _ingest(user_id: str, name: str, body: str):
    await _user(user_id)
    async with db.sessionmaker()() as s:
        return await rag.ingest(s, user_id, name, "text/markdown", body.encode())


async def test_hybrid_search_finds_identifier_and_is_user_scoped():
    await _ingest("u1", "api.md", "## Errors\n\nE-4031: the shipment cannot be modified.\n\n## Auth\n\nUse a bearer key.")
    await _ingest("u2", "other.md", "E-4031 is also mentioned here by another tenant.")
    async with db.sessionmaker()() as s:
        for mode in ("hybrid", "vector", "lexical"):
            hits = await rag.search(s, "u1", "What does E-4031 mean?", k=3, mode=mode)
            assert hits and all(h["filename"] == "api.md" for h in hits)
            assert "E-4031" in hits[0]["text"]


async def test_llm_rerank_reorders_and_survives_bad_output(monkeypatch):
    from app.llm import FakeLLM, LLMResult, set_llm

    hits = [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}]
    set_llm(FakeLLM(script=[LLMResult(json.dumps({"ranking": [2, 0]}))]))
    assert [h["text"] for h in await rag.rerank("q", hits, "llm")] == ["gamma", "alpha", "beta"]
    set_llm(FakeLLM(script=[LLMResult("not json")]))
    assert await rag.rerank("q", hits, "llm") == hits


# ---------------- context engineering + output guard ----------------


def test_compaction_shrinks_old_tool_outputs_only():
    msgs = [{"role": "system", "content": "S" * 100}, {"role": "user", "content": "task"}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "content": "x" * 5000} for i in range(6)]
    out, saved = compact_messages(msgs, budget_chars=12_000, keep_recent=2)
    assert saved > 0 and sum(len(m["content"]) for m in out) <= 12_000 + 5000
    assert out[0] == msgs[0] and out[1] == msgs[1]  # system + user turns untouched
    assert out[-1]["content"] == "x" * 5000 and out[-2]["content"] == "x" * 5000  # recent evidence kept verbatim
    assert "[compacted:" in out[2]["content"]
    same, zero = compact_messages(msgs[:3], budget_chars=1_000_000)
    assert zero == 0 and same == msgs[:3]


def test_output_guard_redacts_credentials():
    text, found = redact_secrets("key sk-proj-abcdefghijklmnopqrstuvwxyz0123 and AKIAABCDEFGHIJKLMNOP done")
    assert "sk-proj" not in text and "AKIA" not in text and {"openai_key", "aws_access_key"} <= set(found)
    assert redact_secrets("nothing secret here") == ("nothing secret here", [])


# ---------------- data files in the sandbox ----------------


async def test_sandbox_reads_mounted_files_and_skips_them_as_images():
    res = await run_python(
        "import pandas as pd; print(pd.read_csv('sales.csv').revenue.sum())",
        timeout_s=30,
        files={"sales.csv": b"region,revenue\nN,10\nS,32\n", "logo.png": b"\x89PNG fake"},
    )
    assert res["exit_code"] == 0, res["stderr"]
    assert res["stdout"].strip() == "42" and res["images"] == []


async def test_python_exec_sees_the_users_uploaded_csv_only():
    await _user("u1")
    await _user("u2")
    async with db.sessionmaker()() as s:
        await rag.ingest(s, "u1", "sales.csv", "text/csv", b"region,revenue\nNorth,5\nSouth,7\n")
        await rag.ingest(s, "u2", "secret.csv", "text/csv", b"a\n1\n")
    code = "import os, pandas as pd; print(sorted(f for f in os.listdir('.') if f.endswith('.csv'))); print(pd.read_csv('sales.csv').revenue.max())"
    out, text, _ = await default_registry().execute("python_exec", {"code": code}, ToolContext("u1", "r1"))
    assert not out.error, text
    assert "['sales.csv']" in text and "7" in text


# ---------------- guest mode, samples, budgets ----------------


async def test_guest_login_samples_and_lower_budget(client):
    r = await client.post("/v1/auth/guest")
    assert r.status_code == 201
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/v1/me", headers=h)).json()
    assert me["email"].endswith("@guest.atlas") and me["daily_limit_usd"] == get_settings().guest_daily_cost_usd
    names = [s["name"] for s in (await client.get("/v1/samples")).json()]
    assert "sales.csv" in names and "northwind-handbook.md" in names
    first = await client.post("/v1/samples/sales.csv", headers=h)
    again = await client.post("/v1/samples/sales.csv", headers=h)
    assert first.status_code == 201 and first.json()["id"] == again.json()["id"]  # idempotent
    assert (await client.post("/v1/samples/..%2F..%2Fconfig.py", headers=h)).status_code == 404


async def test_global_budget_blocks_new_runs(client, monkeypatch):
    from sqlalchemy import update

    from app.models import Run

    h = await signup(client)
    r = await client.post("/v1/runs", json={"message": "hi"}, headers=h)
    await stream_events(client, h, r.json()["run_id"])
    async with db.sessionmaker()() as s:
        await s.execute(update(Run).values(cost_usd=1.0))
        await s.commit()
    monkeypatch.setenv("GLOBAL_DAILY_COST_USD", "0.5")
    monkeypatch.setenv("DAILY_USER_COST_USD", "100")
    get_settings.cache_clear()
    other = await signup(client, email="other@example.com")
    blocked = await client.post("/v1/runs", json={"message": "hi"}, headers=other)
    assert blocked.status_code == 503


async def test_agent_answers_from_uploaded_csv(client):
    h = await signup(client)
    await client.post("/v1/samples/sales.csv", headers=h)
    r = await client.post(
        "/v1/runs", json={"message": "Using my uploaded sales.csv, which region had the highest total revenue?"}, headers=h
    )
    events = await stream_events(client, h, r.json()["run_id"])
    assert any(e["type"] == "tool.call" and e["tool"] == "python_exec" for e in events)
    assert "North" in events[-1]["answer"]


# ---------------- MCP ----------------


async def _mcp(client, headers, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    r = await client.post("/mcp", json=body, headers={**headers, "Accept": "application/json, text/event-stream"})
    return r


async def test_mcp_server_requires_auth_and_runs_tasks(client):
    from app.main import create_app

    app = create_app()
    import httpx

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
            assert (await _mcp(c, {}, "tools/list")).status_code == 401
            h = await signup(c)
            init = await _mcp(
                c, h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
            )
            assert init.status_code == 200, init.text
            tools = await _mcp(c, h, "tools/list", id_=2)
            names = {t["name"] for t in tools.json()["result"]["tools"]}
            assert {"run_task", "search_documents", "list_documents"} <= names
            await c.post("/v1/samples/northwind-api-reference.md", headers=h)
            found = await _mcp(c, h, "tools/call", {"name": "search_documents", "arguments": {"query": "E-4031"}}, id_=3)
            assert "E-4031" in json.dumps(found.json())
            done = await _mcp(c, h, "tools/call", {"name": "run_task", "arguments": {"task": "Calculate 21 * 2"}}, id_=4)
            assert "42" in json.dumps(done.json()), done.text


async def test_mcp_client_wraps_remote_tools(monkeypatch):
    from contextlib import asynccontextmanager

    from mcp.client.client import Client
    from mcp.server.mcpserver import MCPServer

    from app.tools import mcp_client

    remote = MCPServer(name="calc")

    @remote.tool(description="Add two integers")
    def add(a: int, b: int) -> int:
        return a + b

    @asynccontextmanager
    async def fake_open(server):
        async with Client(remote) as c:
            yield c

    monkeypatch.setattr(mcp_client, "open_client", fake_open)
    mcp_client.clear_cache()
    tools = await mcp_client.load_mcp_tools([{"name": "calc", "url": "memory://calc"}, {"name": "broken"}])
    assert [t.name for t in tools] == ["calc__add"]
    assert tools[0].parameters["properties"].keys() >= {"a", "b"}
    out = await tools[0].fn({"a": 2, "b": 40}, ToolContext("u1", "r1"))
    assert "42" in out.text and out.untrusted and not out.error


async def test_effort_presets_change_the_loop(client):
    h = await signup(client)
    events = {}
    for effort in ("fast", "balanced"):
        r = await client.post("/v1/runs", json={"message": "Please calculate 6 * 7", "effort": effort}, headers=h)
        assert r.status_code == 202
        events[effort] = [e["type"] for e in await stream_events(client, h, r.json()["run_id"])]
        assert (await client.get(f"/v1/runs/{r.json()['run_id']}", headers=h)).json()["effort"] == effort
    assert "reflection" not in events["fast"] and "reflection" in events["balanced"]
    assert (await client.post("/v1/runs", json={"message": "x", "effort": "max"}, headers=h)).status_code == 422
    info = (await client.get("/v1/info")).json()
    assert info["mcp_server"] == "/mcp" and "deep" in info["efforts"]
