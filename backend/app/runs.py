"""Run lifecycle: admission (quota) -> dispatch (inline task or Redis queue) -> execution -> persistence.

Execution is decoupled from the HTTP request: the API returns a run id immediately and clients
stream events from the event bus, so runs survive client disconnects and can be resumed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agent.graph import AgentDeps, RunCancelled, run_agent
from app.config import get_settings
from app.infra import get_infra
from app.llm import get_llm
from app.logging import log
from app.metrics import RUN_LATENCY, RUNS, TTFT
from app.models import Conversation, Document, Message, Run, User, utcnow
from app.schemas import RunCreated
from app.tools import ToolContext, build_registry

# Effort presets trade latency and cost against depth. They only ever tighten or loosen the agent loop;
# the per-run USD cap and the daily quotas still apply on top.
EFFORT = {
    "fast": {"max_agent_steps": 3, "max_tool_calls_per_run": 4, "enable_reflection": False},
    "balanced": {},
    "deep": {"max_agent_steps": 14, "max_tool_calls_per_run": 24, "enable_reflection": True},
}
HISTORY_MESSAGES = 20
HISTORY_CHARS = 24_000
_background: set[asyncio.Task] = set()
_arq_pool = None


GUEST_DOMAIN = "guest.atlas"


def is_guest(user: User) -> bool:
    return user.email.endswith("@" + GUEST_DOMAIN)


def daily_limit(user: User) -> float:
    s = get_settings()
    return s.guest_daily_cost_usd if is_guest(user) else s.daily_user_cost_usd


async def check_daily_quota(session: AsyncSession, user: User) -> None:
    s = get_settings()
    since = datetime.now(UTC) - timedelta(days=1)
    spent = await session.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.user_id == user.id, Run.created_at >= since))
    limit = daily_limit(user)
    if float(spent or 0) >= limit:
        raise HTTPException(429, f"Daily spend limit reached (${limit:.2f}). Try again tomorrow.")
    if s.global_daily_cost_usd > 0:  # protects a public deployment's model key from being drained
        total = await session.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.created_at >= since))
        if float(total or 0) >= s.global_daily_cost_usd:
            raise HTTPException(503, "This deployment has used today's model budget. Please try again tomorrow.")


async def start_run(session: AsyncSession, user: User, message: str, conversation_id: str | None, effort: str = "balanced") -> RunCreated:
    """Admission + persistence for a new run, shared by the REST API and the MCP server."""
    await check_daily_quota(session, user)
    if conversation_id:
        conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
        if conv is None:
            raise HTTPException(404, "Conversation not found")
        busy = await session.scalar(select(Run.id).where(Run.conversation_id == conv.id, Run.status.in_(["queued", "running"])))
        if busy:
            raise HTTPException(409, "A run is already in progress for this conversation")
    else:
        conv = Conversation(user_id=user.id, title=" ".join(message.split())[:60] or "New chat")
        session.add(conv)
        await session.flush()
    run = Run(conversation_id=conv.id, user_id=user.id, input=message, effort=effort if effort in EFFORT else "balanced")
    session.add(run)
    await session.flush()
    msg = Message(conversation_id=conv.id, role="user", content=message, run_id=run.id)
    session.add(msg)
    conv.updated_at = datetime.now(UTC)
    await session.commit()
    await dispatch(run.id)
    return RunCreated(run_id=run.id, conversation_id=conv.id, user_message_id=msg.id, events_url=f"/v1/runs/{run.id}/events")


async def dispatch(run_id: str) -> None:
    global _arq_pool
    if get_settings().queue_backend == "arq":
        if _arq_pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        await _arq_pool.enqueue_job("execute_run_job", run_id, _job_id=f"run:{run_id}")
    else:
        task = asyncio.create_task(execute_run(run_id))
        _background.add(task)
        task.add_done_callback(_background.discard)


async def _history(session: AsyncSession, conversation_id: str, before: datetime, exclude_id: str | None) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.created_at <= before)
                .order_by(Message.created_at.desc())
                .limit(HISTORY_MESSAGES + 1)
            )
        )
        .scalars()
        .all()
    )
    hist, used = [], 0
    for m in rows:
        if m.id == exclude_id:
            continue
        if used + len(m.content) > HISTORY_CHARS:
            break
        used += len(m.content)
        hist.append({"role": m.role, "content": m.content})
    return list(reversed(hist))


async def execute_run(run_id: str) -> None:
    infra, settings = get_infra(), get_settings()
    started = time.perf_counter()
    trace: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        event = {**event, "ts": round(time.time(), 3)}
        if event["type"] in ("plan", "tool.call", "tool.result", "reflection", "guard.flag"):
            trace.append({k: v for k, v in event.items() if k != "images"})
        await infra.bus.publish(run_id, event)

    async with db.sessionmaker()() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status not in ("queued",):
            log.warning("run.skip", run_id=run_id, status=getattr(run, "status", None))
            return
        run.status = "running"
        await session.commit()
        user_msg = (await session.execute(select(Message).where(Message.run_id == run_id, Message.role == "user"))).scalars().first()
        history = await _history(session, run.conversation_id, run.created_at, user_msg.id if user_msg else None)
        query, user_id, conversation_id = run.input, run.user_id, run.conversation_id
        settings = settings.model_copy(update=EFFORT.get(run.effort or "balanced", {}))
        filenames = (
            (
                await session.execute(
                    select(Document.filename).where(Document.user_id == user_id).order_by(Document.created_at.desc()).limit(30)
                )
            )
            .scalars()
            .all()
        )

    log.info("run.started", run_id=run_id, user_id=user_id)
    await emit({"type": "run.started", "run_id": run_id})
    deps = AgentDeps(
        llm=get_llm(),
        tools=await build_registry(),
        settings=settings,
        ctx=ToolContext(user_id=user_id, run_id=run_id),
        emit=emit,
        is_cancelled=lambda: infra.cancel.is_cancelled(run_id),
    )
    status, error, state = "completed", None, None
    try:
        state = await run_agent(history, query, deps, files=list(dict.fromkeys(filenames)))
    except RunCancelled:
        status = "cancelled"
    except Exception as e:  # noqa: BLE001
        status, error = "failed", f"{type(e).__name__}: {str(e)[:500]}"
        log.exception("run.failed", run_id=run_id)

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = (state or {}).get("usage", {})
    async with db.sessionmaker()() as session:
        message_id = None
        if status == "completed" and state:
            msg = Message(
                conversation_id=conversation_id,
                role="assistant",
                content=state["final"],
                run_id=run_id,
                meta={
                    "trace": trace,
                    "images": state.get("images", []),
                    "sources": state.get("sources", []),
                    "usage": usage,
                    "latency_ms": latency_ms,
                    "stop_reason": state.get("stop_reason"),
                },
            )
            session.add(msg)
            await session.flush()
            message_id = msg.id
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                output=(state or {}).get("final"),
                error=error,
                steps=(state or {}).get("steps", 0),
                tool_calls=(state or {}).get("tool_calls", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cost_usd=usage.get("cost_usd", 0.0),
                latency_ms=latency_ms,
                models_used=usage.get("models", []),
                finished_at=utcnow(),
            )
        )
        await session.commit()

    RUNS.labels(status).inc()
    RUN_LATENCY.observe(latency_ms / 1000)
    if usage.get("ttft_s"):
        TTFT.observe(usage["ttft_s"])
    log.info("run.finished", run_id=run_id, status=status, latency_ms=latency_ms, cost_usd=usage.get("cost_usd", 0))
    if status == "completed":
        await emit(
            {
                "type": "run.completed",
                "message_id": message_id,
                "answer": state["final"],
                "usage": usage,
                "latency_ms": latency_ms,
                "steps": state.get("steps", 0),
                "tool_calls": state.get("tool_calls", 0),
                "stop_reason": state.get("stop_reason"),
            }
        )
    elif status == "cancelled":
        await emit({"type": "run.cancelled"})
    else:
        await emit({"type": "run.failed", "error": "The agent hit an error. Please retry." if settings.environment == "prod" else error})


async def recover_stale_runs(max_age_minutes: int = 15) -> int:
    """Mark runs orphaned by a crash/redeploy as failed so the UI never spins forever."""
    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    async with db.sessionmaker()() as session:
        res = await session.execute(
            update(Run)
            .where(Run.status.in_(["queued", "running"]), Run.created_at < cutoff)
            .values(status="failed", error="interrupted (worker restart)", finished_at=utcnow())
        )
        await session.commit()
        return res.rowcount or 0
