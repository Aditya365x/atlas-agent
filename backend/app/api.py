"""HTTP API (v1). All resources are scoped to the authenticated user."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app import rag
from app.config import get_settings
from app.db import get_session
from app.infra import TERMINAL_EVENTS, get_infra
from app.models import Conversation, Document, Run, User
from app.runs import GUEST_DOMAIN, daily_limit, start_run
from app.schemas import (
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    Credentials,
    DocumentOut,
    MeOut,
    RunCreate,
    RunCreated,
    RunOut,
    StatsOut,
    TokenOut,
)
from app.security import create_token, current_user, decode_token, hash_password, verify_password

router = APIRouter(prefix="/v1")


async def rate_limited(request: Request, user: User = Depends(current_user)) -> User:
    ok, retry = await get_infra().limiter.hit(f"runs:{user.id}", get_settings().rate_limit_per_minute)
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers={"Retry-After": str(retry)})
    return user


@router.get("/info", tags=["ops"])
async def info():
    """Public capabilities of this deployment (the UI shows the model and the MCP endpoint)."""
    s = get_settings()
    return {
        "version": "1.1.0",
        "model": "offline demo model" if s.llm_mode == "fake" else s.model_main,
        "fallbacks": s.model_fallbacks,
        "retrieval": {"mode": s.retrieval_mode, "rerank": s.rerank, "embeddings": s.embedding_model or "local hashing"},
        "mcp_server": "/mcp" if s.enable_mcp_server else None,
        "mcp_clients": [m.get("name") for m in s.mcp_servers],
        "sandbox": s.sandbox_backend,
        "efforts": ["fast", "balanced", "deep"],
    }


# ----------------------------------- auth --------------------------------------------


@router.post("/auth/register", response_model=TokenOut, status_code=201, tags=["auth"])
async def register(body: Credentials, request: Request, session: AsyncSession = Depends(get_session)):
    ip = request.client.host if request.client else "unknown"
    ok, retry = await get_infra().limiter.hit(f"register:{ip}", 10, 3600)
    if not ok:
        raise HTTPException(429, "Too many signups from this IP", headers={"Retry-After": str(retry)})
    email = body.email.lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    await session.commit()
    return TokenOut(access_token=create_token(user.id), user_id=user.id, email=user.email)


@router.post("/auth/guest", response_model=TokenOut, status_code=201, tags=["auth"])
async def guest(request: Request, session: AsyncSession = Depends(get_session)):
    """One-click demo account with a smaller daily budget, so visitors can try Atlas without signing up."""
    ip = request.client.host if request.client else "unknown"
    ok, retry = await get_infra().limiter.hit(f"guest:{ip}", 5, 3600)
    if not ok:
        raise HTTPException(429, "Too many guest sessions from this IP", headers={"Retry-After": str(retry)})
    user = User(email=f"guest-{secrets.token_hex(5)}@{GUEST_DOMAIN}", password_hash=hash_password(secrets.token_urlsafe(24)))
    session.add(user)
    await session.commit()
    return TokenOut(access_token=create_token(user.id), user_id=user.id, email=user.email)


@router.post("/auth/login", response_model=TokenOut, tags=["auth"])
async def login(body: Credentials, request: Request, session: AsyncSession = Depends(get_session)):
    ok, retry = await get_infra().limiter.hit(f"login:{body.email.lower()}", 10, 300)  # brute-force protection
    if not ok:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
    user = (await session.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return TokenOut(access_token=create_token(user.id), user_id=user.id, email=user.email)


@router.get("/me", response_model=MeOut, tags=["auth"])
async def me(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    since = datetime.now(UTC) - timedelta(days=1)
    spent = await session.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.user_id == user.id, Run.created_at >= since))
    return MeOut(id=user.id, email=user.email, spend_24h_usd=round(float(spent or 0), 6), daily_limit_usd=daily_limit(user))


# ----------------------------------- conversations -----------------------------------


async def _own_conversation(session: AsyncSession, cid: str, user: User, with_messages: bool = False) -> Conversation:
    q = select(Conversation).where(Conversation.id == cid, Conversation.user_id == user.id)
    if with_messages:
        q = q.options(selectinload(Conversation.messages))
    conv = (await session.execute(q)).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("/conversations", response_model=list[ConversationOut], tags=["conversations"])
async def list_conversations(
    limit: int = Query(50, le=200), user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    rows = await session.execute(
        select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc()).limit(limit)
    )
    return rows.scalars().all()


@router.get("/conversations/{cid}", response_model=ConversationDetail, tags=["conversations"])
async def get_conversation(cid: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await _own_conversation(session, cid, user, with_messages=True)


@router.patch("/conversations/{cid}", response_model=ConversationOut, tags=["conversations"])
async def rename_conversation(
    cid: str, body: ConversationUpdate, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
):
    conv = await _own_conversation(session, cid, user)
    conv.title = body.title
    await session.commit()
    return conv


@router.delete("/conversations/{cid}", status_code=204, tags=["conversations"])
async def delete_conversation(cid: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    conv = await _own_conversation(session, cid, user)
    await session.execute(delete(Run).where(Run.conversation_id == conv.id))
    await session.delete(conv)
    await session.commit()
    return Response(status_code=204)


# ----------------------------------- runs --------------------------------------------


@router.post("/runs", response_model=RunCreated, status_code=202, tags=["runs"])
async def create_run(body: RunCreate, user: User = Depends(rate_limited), session: AsyncSession = Depends(get_session)):
    return await start_run(session, user, body.message, body.conversation_id, body.effort)


async def _own_run(session: AsyncSession, run_id: str, user_id: str) -> Run:
    run = await session.get(Run, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}", response_model=RunOut, tags=["runs"])
async def get_run(run_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await _own_run(session, run_id, user.id)


@router.post("/runs/{run_id}/cancel", status_code=202, tags=["runs"])
async def cancel_run(run_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    run = await _own_run(session, run_id, user.id)
    if run.status in ("queued", "running"):
        await get_infra().cancel.cancel(run_id)
    return {"status": "cancelling" if run.status in ("queued", "running") else run.status}


@router.get("/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: str,
    request: Request,
    token: str | None = Query(None, description="JWT for EventSource clients that cannot set headers"),
    authorization: str | None = Header(None),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_session),
):
    """Server-Sent Events stream of a run. Replays from the start (or from Last-Event-ID) then follows live."""
    raw = token or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    if not raw:
        raise HTTPException(401, "Missing token")
    run = await _own_run(session, run_id, decode_token(raw))
    finished = run.status in ("completed", "failed", "cancelled")
    run_status, run_output, run_error = run.status, run.output, run.error
    await session.close()  # don't hold a DB connection for the life of the stream

    async def gen():
        got_terminal = False
        try:
            async for eid, ev in get_infra().bus.subscribe(run_id, last_event_id):
                if await request.is_disconnected():
                    return
                yield {"id": eid, "event": "message", "data": json.dumps(ev, default=str)}
                if ev["type"] in TERMINAL_EVENTS:
                    got_terminal = True
                    return
        except TimeoutError:
            pass
        if finished and not got_terminal:  # stream expired (e.g. >24h) -> synthesize the terminal event from the DB
            kind = {"completed": "run.completed", "failed": "run.failed", "cancelled": "run.cancelled"}[run_status]
            yield {"event": "message", "data": json.dumps({"type": kind, "answer": run_output, "error": run_error})}

    return EventSourceResponse(gen(), ping=15, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@router.get("/stats", response_model=StatsOut, tags=["runs"])
async def stats(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Run).where(Run.user_id == user.id).order_by(Run.created_at.desc()).limit(1000))).scalars().all()
    lat = sorted(r.latency_ms for r in rows if r.latency_ms is not None and r.status == "completed")
    return StatsOut(
        runs=len(rows),
        completed=sum(r.status == "completed" for r in rows),
        failed=sum(r.status == "failed" for r in rows),
        avg_latency_ms=round(sum(lat) / len(lat), 1) if lat else None,
        p95_latency_ms=float(lat[min(len(lat) - 1, int(0.95 * len(lat)))]) if lat else None,
        total_cost_usd=round(sum(r.cost_usd for r in rows), 6),
        total_tokens=sum(r.prompt_tokens + r.completion_tokens for r in rows),
        tool_calls=sum(r.tool_calls for r in rows),
    )


# ----------------------------------- files -------------------------------------------


@router.post("/files", response_model=DocumentOut, status_code=201, tags=["files"])
async def upload_file(file: UploadFile = File(...), user: User = Depends(rate_limited), session: AsyncSession = Depends(get_session)):
    name = Path(file.filename or "upload.txt").name[:300]
    if Path(name).suffix.lower() not in rag.SUPPORTED:
        raise HTTPException(415, f"Unsupported file type. Allowed: {sorted(rag.SUPPORTED)}")
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File exceeds {get_settings().max_upload_mb} MB")
    try:
        return await rag.ingest(session, user.id, name, file.content_type or "application/octet-stream", data)
    except Exception as e:  # corrupt PDF, etc.
        raise HTTPException(422, f"Could not process file: {type(e).__name__}") from e


SAMPLES_DIR = Path(__file__).parent / "samples"


@router.get("/samples", tags=["files"])
async def list_samples():
    return [{"name": p.name, "size_bytes": p.stat().st_size} for p in sorted(SAMPLES_DIR.glob("*")) if p.suffix in rag.SUPPORTED]


@router.post("/samples/{name}", response_model=DocumentOut, status_code=201, tags=["files"])
async def load_sample(name: str, user: User = Depends(rate_limited), session: AsyncSession = Depends(get_session)):
    """Copy a bundled sample file (sales data, company handbook...) into the caller's workspace."""
    path = SAMPLES_DIR / Path(name).name
    if not path.is_file() or path.suffix not in rag.SUPPORTED:
        raise HTTPException(404, "Sample not found")
    existing = await session.scalar(select(Document).where(Document.user_id == user.id, Document.filename == path.name))
    if existing:
        return existing
    return await rag.ingest(session, user.id, path.name, "text/plain", path.read_bytes())


@router.get("/files", response_model=list[DocumentOut], tags=["files"])
async def list_files(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return (await session.execute(select(Document).where(Document.user_id == user.id).order_by(Document.created_at.desc()))).scalars().all()


@router.delete("/files/{doc_id}", status_code=204, tags=["files"])
async def delete_file(doc_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    doc = await session.get(Document, doc_id)
    if doc is None or doc.user_id != user.id:
        raise HTTPException(404, "File not found")
    from app.models import Chunk

    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    await session.delete(doc)
    await session.commit()
    return Response(status_code=204)
