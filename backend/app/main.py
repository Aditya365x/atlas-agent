"""FastAPI application: lifespan wiring, middleware (request ids, metrics, security headers), health probes."""

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app import db
from app.api import router
from app.config import get_settings
from app.infra import get_infra
from app.logging import configure_logging, log
from app.metrics import HTTP_LATENCY, HTTP_REQUESTS
from app.runs import recover_stale_runs


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level, json=s.environment != "dev")
    if s.environment == "prod" and s.jwt_secret.startswith("change-me"):
        raise RuntimeError("JWT_SECRET must be set in production")
    await db.init_db()
    recovered = await recover_stale_runs()
    log.info(
        "startup",
        env=s.environment,
        llm_mode=s.llm_mode,
        model=s.model_main,
        queue=s.queue_backend,
        sandbox=s.sandbox_backend,
        redis=bool(s.redis_url),
        recovered_runs=recovered,
    )
    yield
    await get_infra().close()
    await db.dispose_db()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Atlas Agent API",
        version="1.0.0",
        description="Production-grade autonomous agent: planning, tool use, sandboxed code, RAG, streaming.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("http.unhandled", path=request.url.path)
            response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        elapsed = time.perf_counter() - start
        if not path.endswith("/events"):  # streaming responses would skew latency
            HTTP_LATENCY.labels(path).observe(elapsed)
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if path not in ("/healthz", "/metrics"):
            log.info("http", method=request.method, path=path, status=response.status_code, ms=int(elapsed * 1000))
        return response

    app.include_router(router)

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz():
        checks = {"redis": await get_infra().ping()}
        try:
            async with db.get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            checks["database"] = False
        ok = all(checks.values())
        return JSONResponse({"status": "ready" if ok else "degraded", **checks}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
