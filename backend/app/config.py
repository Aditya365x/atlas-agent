"""Typed, env-driven configuration. Every knob is overridable via environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    app_name: str = "Atlas Agent"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- security ---
    jwt_secret: str = "change-me-in-production-please-32b+"
    jwt_ttl_minutes: int = 60 * 24 * 7

    # --- storage ---
    database_url: str = "sqlite+aiosqlite:///./atlas.db"
    # Connection budget per process: keep (api_processes + workers) * (pool + overflow) < Postgres max_connections
    db_pool_size: int = 5
    db_max_overflow: int = 5
    redis_url: str | None = None  # when unset: in-memory event bus, rate limiter and task runner

    # --- execution ---
    queue_backend: Literal["inline", "arq"] = "inline"  # arq = durable Redis-backed worker

    # --- LLM routing (any LiteLLM model string works: openai/..., anthropic/..., gemini/..., deepseek/...) ---
    llm_mode: Literal["live", "fake"] = "live"  # fake = deterministic offline model for tests / CI / demos
    model_main: str = "gemini/gemini-2.5-flash"
    model_planner: str | None = None  # defaults to model_main
    model_fallbacks: list[str] = Field(default_factory=list)  # tried in order if the primary fails
    embedding_model: str | None = None  # None -> local hashing embeddings (no API needed)
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # --- agent budgets (guardrails against runaway loops and bills) ---
    max_agent_steps: int = 8
    max_tool_calls_per_run: int = 12
    max_run_cost_usd: float = 0.25
    daily_user_cost_usd: float = 2.0
    enable_reflection: bool = True

    # --- tools ---
    tavily_api_key: str | None = None
    sandbox_backend: Literal["local", "http", "e2b"] = "local"
    sandbox_url: str = "http://sandbox:8080"
    sandbox_token: str | None = None
    e2b_api_key: str | None = None
    sandbox_timeout_s: int = 20
    max_upload_mb: int = 15

    # --- rate limiting ---
    rate_limit_per_minute: int = 20

    # --- observability ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        """Accept the plain URLs PaaS providers hand out (postgres://..., ?sslmode=require)."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix) :]
        return v.replace("sslmode=", "ssl=")

    @property
    def planner_model(self) -> str:
        return self.model_planner or self.model_main


@lru_cache
def get_settings() -> Settings:
    return Settings()
