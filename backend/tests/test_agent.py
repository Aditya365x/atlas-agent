"""Agent state-machine tests with a scripted model: no network, fully deterministic."""

import json

from app.agent.graph import AgentDeps, RunCancelled, parse_json_obj, run_agent
from app.config import get_settings
from app.llm import FakeLLM, LLMResult, ToolCall
from app.tools import ToolContext, default_registry


def make_deps(llm, events, cancelled=False, **overrides):
    s = get_settings().model_copy(update=overrides)

    async def emit(e):
        events.append(e)

    async def is_cancelled():
        return cancelled

    return AgentDeps(llm=llm, tools=default_registry(), settings=s, ctx=ToolContext("u1", "r1"), emit=emit, is_cancelled=is_cancelled)


def plan(needs=True):
    return LLMResult(json.dumps({"needs_tools": needs, "steps": ["compute", "answer"]}), cost_usd=0.001)


def call(name, **args):
    return LLMResult(tool_calls=[ToolCall(f"c_{name}", name, json.dumps(args))], cost_usd=0.002)


async def test_full_loop_plan_tool_reflect_revise():
    llm = FakeLLM(
        [
            plan(),
            call("python_exec", code="print(17*23)"),
            LLMResult("17 x 23 = 400", cost_usd=0.002),  # wrong draft
            LLMResult(json.dumps({"score": 3, "feedback": "The tool output says 391, not 400."})),
            LLMResult("17 x 23 = **391**", cost_usd=0.002),  # revised
        ]
    )
    events = []
    state = await run_agent([], "What is 17*23?", make_deps(llm, events))
    types = [e["type"] for e in events]
    assert types[0] == "plan"
    assert types.index("tool.call") < types.index("tool.result") < types.index("reflection")
    assert "answer.reset" in types  # revision clears the streamed draft
    assert state["final"] == "17 x 23 = **391**"
    assert state["revisions"] == 1 and state["tool_calls"] == 1
    tool_result = next(e for e in events if e["type"] == "tool.result")
    assert tool_result["ok"] and "391" in tool_result["preview"]
    assert abs(state["usage"]["cost_usd"] - 0.007) < 1e-9
    assert any(m["role"] == "tool" for m in llm.calls[2]["messages"])  # tool output fed back to the model


async def test_fast_path_skips_tools_and_reflection():
    llm = FakeLLM([plan(needs=False), LLMResult("Hello Adi!")])
    events = []
    state = await run_agent([], "hi", make_deps(llm, events))
    assert state["final"] == "Hello Adi!"
    assert llm.calls[1]["tools"] == []  # no tool schemas sent -> cheaper/faster
    assert not any(e["type"] == "reflection" for e in events)
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello Adi!"


async def test_parallel_tool_calls():
    two = LLMResult(
        tool_calls=[
            ToolCall("a", "python_exec", json.dumps({"code": "print(1)"})),
            ToolCall("b", "python_exec", json.dumps({"code": "print(2)"})),
        ]
    )
    llm = FakeLLM([plan(), two, LLMResult("1 and 2"), LLMResult(json.dumps({"score": 9, "feedback": ""}))])
    events = []
    state = await run_agent([], "run two things", make_deps(llm, events))
    assert state["tool_calls"] == 2
    assert {e["id"] for e in events if e["type"] == "tool.result"} == {"a", "b"}


async def test_step_budget_forces_final_answer():
    looping = [call("python_exec", code="print('again')") for _ in range(10)]
    llm = FakeLLM([plan(), *looping[:3], LLMResult("Best effort answer.")])
    events = []
    state = await run_agent([], "loop forever", make_deps(llm, events, max_agent_steps=3, max_tool_calls_per_run=50))
    assert state["final"] == "Best effort answer."
    assert state["stop_reason"] == "step_budget"
    assert llm.calls[-1]["tools"] == []  # final call is made without tools


async def test_cost_budget_forces_answer():
    pricey = LLMResult(json.dumps({"needs_tools": True, "steps": ["x"]}), cost_usd=1.0)
    llm = FakeLLM([pricey, LLMResult("Answer under budget.")])
    state = await run_agent([], "expensive", make_deps(llm, [], max_run_cost_usd=0.5))
    assert state["stop_reason"] == "cost_budget" and llm.calls[-1]["tools"] == []


async def test_bad_tool_arguments_do_not_crash():
    bad = LLMResult(tool_calls=[ToolCall("x", "python_exec", "{not json")])
    llm = FakeLLM([plan(), bad, LLMResult("Recovered."), LLMResult(json.dumps({"score": 9}))])
    events = []
    state = await run_agent([], "q", make_deps(llm, events))
    assert state["final"] == "Recovered."
    assert next(e for e in events if e["type"] == "tool.result")["ok"] is False


async def test_cancellation():
    llm = FakeLLM([plan()])
    try:
        await run_agent([], "q", make_deps(llm, [], cancelled=True))
        raise AssertionError("expected cancellation")
    except RunCancelled:
        pass


def test_parse_json_handles_fences_and_chatter():
    assert parse_json_obj('Sure!\n```json\n{"score": 8, "feedback": ""}\n```') == {"score": 8, "feedback": ""}
    assert parse_json_obj("no json here") is None
