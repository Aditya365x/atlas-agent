"""Router resilience: retries on transient errors, falls back across providers, gives up cleanly."""

import litellm
import pytest

from app.config import get_settings
from app.llm import LiteLLMRouter, LLMResult, LLMUnavailable


def router(**overrides):
    s = get_settings().model_copy(update={"model_main": "primary/m", "model_fallbacks": ["backup/m"], "llm_max_retries": 2, **overrides})
    return LiteLLMRouter(s)


async def test_retries_transient_errors(monkeypatch):
    r = router()
    calls = []

    async def flaky(model, *a, **k):
        calls.append(model)
        if len(calls) < 3:
            raise litellm.RateLimitError("slow down", llm_provider="x", model=model)
        return LLMResult("ok", model=model)

    monkeypatch.setattr(r, "_stream_once", flaky)
    monkeypatch.setattr("app.llm.asyncio.sleep", _no_sleep)
    res = await r.complete([{"role": "user", "content": "hi"}])
    assert res.content == "ok" and calls == ["primary/m"] * 3


async def test_falls_back_on_non_retryable(monkeypatch):
    r = router()
    calls = []

    async def auth_fail_then_ok(model, *a, **k):
        calls.append(model)
        if model == "primary/m":
            raise litellm.AuthenticationError("bad key", llm_provider="x", model=model)
        return LLMResult("from backup", model=model)

    monkeypatch.setattr(r, "_stream_once", auth_fail_then_ok)
    res = await r.complete([{"role": "user", "content": "hi"}])
    assert res.model == "backup/m" and calls == ["primary/m", "backup/m"]


async def test_all_models_down(monkeypatch):
    r = router(llm_max_retries=0)

    async def down(model, *a, **k):
        raise litellm.ServiceUnavailableError("down", llm_provider="x", model=model)

    monkeypatch.setattr(r, "_stream_once", down)
    with pytest.raises(LLMUnavailable):
        await r.complete([{"role": "user", "content": "hi"}])


async def _no_sleep(*_):
    return None


def test_openrouter_prices_are_registered_so_budgets_work():
    import litellm

    from app.llm import register_openrouter_prices

    catalog = [
        {
            "id": "acme/new-model-x",
            "pricing": {"prompt": "0.0000001", "completion": "0.00000035"},
            "context_length": 1000,
            "supported_parameters": ["tools"],
        }
    ]
    done = register_openrouter_prices(["openrouter/acme/new-model-x", "openrouter/acme/missing", "gemini/x"], fetch=lambda: catalog)
    assert done == ["openrouter/acme/new-model-x"]
    assert litellm.model_cost["openrouter/acme/new-model-x"]["output_cost_per_token"] == pytest.approx(3.5e-7)
    assert register_openrouter_prices(["openrouter/acme/other"], fetch=lambda: 1 / 0) == []  # lookup failures never raise
