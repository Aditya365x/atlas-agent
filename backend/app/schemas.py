from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


class MeOut(BaseModel):
    id: str
    email: str
    spend_24h_usd: float
    daily_limit_usd: float


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    role: str
    content: str
    run_id: str | None = None
    meta: dict[str, Any] = {}
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class RunCreate(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = None


class RunCreated(BaseModel):
    run_id: str
    conversation_id: str
    user_message_id: str
    events_url: str


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    status: str
    input: str
    output: str | None
    error: str | None
    steps: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int | None
    models_used: list[str]
    created_at: datetime
    finished_at: datetime | None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    content_type: str
    num_chunks: int
    status: str
    created_at: datetime


class StatsOut(BaseModel):
    runs: int
    completed: int
    failed: int
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    total_cost_usd: float
    total_tokens: int
    tool_calls: int
