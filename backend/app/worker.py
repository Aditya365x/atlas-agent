"""Durable background worker (arq on Redis). Scale horizontally: `arq app.worker.WorkerSettings`.

Jobs are idempotent per run id (the run state machine skips anything not `queued`), so retries and
duplicate deliveries are safe.
"""

from arq.connections import RedisSettings

from app import db
from app.config import get_settings
from app.logging import configure_logging, log
from app.runs import execute_run, recover_stale_runs


async def execute_run_job(ctx: dict, run_id: str) -> None:
    await execute_run(run_id)


async def startup(ctx: dict) -> None:
    s = get_settings()
    configure_logging(s.log_level, json=s.environment != "dev")
    await db.init_db()
    log.info("worker.startup", recovered=await recover_stale_runs())


async def shutdown(ctx: dict) -> None:
    await db.dispose_db()


class WorkerSettings:
    functions = [execute_run_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://localhost:6379")
    max_jobs = 20
    job_timeout = 600
    max_tries = 1  # agent runs have side effects (tool calls); never auto-replay
