"""Code-execution microservice. Runs as a non-root user, on a Docker network with NO internet egress,
with a read-only root filesystem (only /tmp is writable). The API reaches it over the internal network."""

import base64
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from sandbox_runner import run_python

app = FastAPI(title="Atlas sandbox", docs_url=None, redoc_url=None)
TOKEN = os.environ.get("SANDBOX_TOKEN")


class RunRequest(BaseModel):
    code: str = Field(max_length=20_000)
    timeout_s: int = Field(20, ge=1, le=120)
    files: dict[str, str] = Field(default_factory=dict)  # name -> base64 data files


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/run")
async def run(req: RunRequest, x_sandbox_token: str | None = Header(None)):
    if TOKEN and x_sandbox_token != TOKEN:
        raise HTTPException(401, "bad token")
    files = {name: base64.b64decode(b64) for name, b64 in list(req.files.items())[:20]}
    return await run_python(req.code, req.timeout_s, files)
