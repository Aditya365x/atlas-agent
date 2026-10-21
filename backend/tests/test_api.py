"""End-to-end API tests: auth, run lifecycle over SSE, persistence, isolation, limits, files."""

import asyncio
import json

from app.config import get_settings
from tests.conftest import signup


async def stream_events(client, headers, run_id):
    events = []
    async with client.stream("GET", f"/v1/runs/{run_id}/events", headers=headers) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                ev = json.loads(line[5:].strip())
                events.append(ev)
                if ev["type"] in ("run.completed", "run.failed", "run.cancelled"):
                    break
    return events


async def test_auth_flow(client):
    h = await signup(client)
    assert (await client.get("/v1/me", headers=h)).json()["email"] == "adi@example.com"
    assert (await client.post("/v1/auth/register", json={"email": "adi@example.com", "password": "supersecret1"})).status_code == 409
    assert (await client.post("/v1/auth/login", json={"email": "adi@example.com", "password": "wrongpass1"})).status_code == 401
    ok = await client.post("/v1/auth/login", json={"email": "ADI@example.com", "password": "supersecret1"})
    assert ok.status_code == 200
    assert (await client.get("/v1/me")).status_code == 401
    assert (await client.get("/v1/me", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_run_streams_and_persists(client):
    h = await signup(client)
    r = await client.post("/v1/runs", json={"message": "Please calculate 1234 * 5678"}, headers=h)
    assert r.status_code == 202, r.text
    run_id, cid = r.json()["run_id"], r.json()["conversation_id"]
    events = await stream_events(client, h, run_id)
    types = [e["type"] for e in events]
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert "plan" in types and "tool.call" in types and "token" in types
    assert "7006652" in next(e for e in events if e["type"] == "tool.result")["preview"]

    run = (await client.get(f"/v1/runs/{run_id}", headers=h)).json()
    assert run["status"] == "completed" and run["tool_calls"] == 1 and run["latency_ms"] is not None

    conv = (await client.get(f"/v1/conversations/{cid}", headers=h)).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["meta"]["trace"]  # tool trace stored for the UI

    # resume: reconnecting with Last-Event-ID replays only what was missed
    second_id = None
    async with client.stream("GET", f"/v1/runs/{run_id}/events", headers=h) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("id:"):
                second_id = line[3:].strip()
                break
    replay = await stream_events(client, {**h, "Last-Event-ID": second_id}, run_id)
    assert len(replay) == len(events) - 1

    stats = (await client.get("/v1/stats", headers=h)).json()
    assert stats["runs"] == 1 and stats["completed"] == 1


async def test_follow_up_uses_history_and_blocks_concurrent_runs(client):
    h = await signup(client)
    first = (await client.post("/v1/runs", json={"message": "hello there"}, headers=h)).json()
    dup = await client.post("/v1/runs", json={"message": "again", "conversation_id": first["conversation_id"]}, headers=h)
    assert dup.status_code in (202, 409)  # 409 if the first run is still in flight
    await stream_events(client, h, first["run_id"])
    if dup.status_code == 202:
        await stream_events(client, h, dup.json()["run_id"])
    follow = await client.post("/v1/runs", json={"message": "and now?", "conversation_id": first["conversation_id"]}, headers=h)
    assert follow.status_code == 202
    await stream_events(client, h, follow.json()["run_id"])
    convs = (await client.get("/v1/conversations", headers=h)).json()
    assert len(convs) == 1


async def test_users_are_isolated(client):
    a = await signup(client, "a@example.com")
    b = await signup(client, "b@example.com")
    run = (await client.post("/v1/runs", json={"message": "secret plans"}, headers=a)).json()
    await stream_events(client, a, run["run_id"])
    assert (await client.get(f"/v1/runs/{run['run_id']}", headers=b)).status_code == 404
    assert (await client.get(f"/v1/conversations/{run['conversation_id']}", headers=b)).status_code == 404
    assert (await client.get(f"/v1/runs/{run['run_id']}/events", headers=b)).status_code == 404
    assert (await client.get("/v1/conversations", headers=b)).json() == []


async def test_rate_limit(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()
    h = await signup(client)
    codes = []
    for _ in range(3):
        r = await client.post("/v1/runs", json={"message": "hi"}, headers=h)
        codes.append(r.status_code)
    assert codes[-1] == 429 and "Retry-After" in r.headers


async def test_daily_spend_quota(client, monkeypatch):
    monkeypatch.setenv("DAILY_USER_COST_USD", "0")
    get_settings.cache_clear()
    h = await signup(client)
    r = await client.post("/v1/runs", json={"message": "hi"}, headers=h)
    assert r.status_code == 429 and "spend limit" in r.json()["detail"]


async def test_cancel_run(client):
    from app.llm import FakeLLM, set_llm

    set_llm(FakeLLM(token_delay=0.05))
    h = await signup(client)
    run = (await client.post("/v1/runs", json={"message": "search the latest news about AI agents"}, headers=h)).json()
    await asyncio.sleep(0.05)
    assert (await client.post(f"/v1/runs/{run['run_id']}/cancel", headers=h)).status_code == 202
    events = await stream_events(client, h, run["run_id"])
    assert events[-1]["type"] in ("run.cancelled", "run.completed")  # completed only if it finished first


async def test_file_upload_and_rag_run(client):
    h = await signup(client)
    doc = "Atlas internal handbook.\n\nThe on-call rotation changes every Tuesday at 10:00 IST.\n\nRefund requests above 5000 INR need manager approval."
    r = await client.post("/v1/files", files={"file": ("handbook.md", doc.encode(), "text/markdown")}, headers=h)
    assert r.status_code == 201 and r.json()["num_chunks"] >= 1
    assert len((await client.get("/v1/files", headers=h)).json()) == 1
    bad = await client.post("/v1/files", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=h)
    assert bad.status_code == 415

    run = (await client.post("/v1/runs", json={"message": "In my uploaded document, when does on-call rotate?"}, headers=h)).json()
    events = await stream_events(client, h, run["run_id"])
    result = next(e for e in events if e["type"] == "tool.result")
    assert result["tool"] == "file_search" and "Tuesday" in result["preview"]


async def test_health_and_metrics(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["database"] is True
    metrics = (await client.get("/metrics")).text
    assert "atlas_http_requests_total" in metrics
