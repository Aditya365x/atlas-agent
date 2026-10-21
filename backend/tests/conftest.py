import os
import tempfile

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "LLM_MODE": "fake",
        "QUEUE_BACKEND": "inline",
        "SANDBOX_BACKEND": "local",
        "RATE_LIMIT_PER_MINUTE": "1000",
    }
)
os.environ.pop("REDIS_URL", None)
if os.environ.get("TEST_REDIS_URL"):  # integration mode: exercise Redis Streams / rate limiter paths
    os.environ["REDIS_URL"] = os.environ["TEST_REDIS_URL"]

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.infra import reset_infra  # noqa: E402
from app.llm import set_llm  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp}/test.db"))
    get_settings.cache_clear()
    reset_infra()
    set_llm(None)
    await db.dispose_db()
    if "TEST_DATABASE_URL" in os.environ:  # postgres integration mode: start from a clean schema
        from app.models import Base

        async with db.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await db.init_db()
    yield
    await db.dispose_db()
    get_settings.cache_clear()
    reset_infra()
    set_llm(None)


@pytest.fixture
async def client():
    from app.main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        yield c


async def signup(client, email="adi@example.com", password="supersecret1") -> dict:
    r = await client.post("/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
