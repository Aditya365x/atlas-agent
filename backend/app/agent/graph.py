"""The agent as an explicit LangGraph state machine.

    plan ──► agent ──(tool calls)──► tools ──┐
               ▲  │                          │
               │  └──(draft)──► reflect ─────┤ (low score: revise once)
               └─────────────────────────────┘
                                   └──► finalize ──► END

Every transition streams typed events (plan, token, tool.call, tool.result, reflection ...) through
`emit`, enforces step / tool-call / USD budgets, and checks a cancellation flag between steps.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from app.agent.prompts import CRITIC_PROMPT, PLANNER_PROMPT, system_prompt
from app.config import Settings
from app.llm import LLM, LLMResult
from app.tools.base import ToolContext, ToolRegistry

Emit = Callable[[dict[str, Any]], Awaitable[None]]


class RunCancelled(Exception):
    pass


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    query: str
    plan: list[str]
    needs_tools: bool
    steps: int
    tool_calls: int
    revisions: int
    force_answer: bool
    draft: str
    final: str
    stop_reason: str
    usage: dict[str, Any]
    images: list[dict[str, str]]
    sources: list[dict[str, str]]


@dataclass
class AgentDeps:
    llm: LLM
    tools: ToolRegistry
    settings: Settings
    ctx: ToolContext
    emit: Emit
    is_cancelled: Callable[[], Awaitable[bool]]


def _deps(config: RunnableConfig) -> AgentDeps:
    return config["configurable"]["deps"]


def _add_usage(usage: dict[str, Any], r: LLMResult) -> dict[str, Any]:
    u = dict(usage or {})
    u["prompt_tokens"] = u.get("prompt_tokens", 0) + r.prompt_tokens
    u["completion_tokens"] = u.get("completion_tokens", 0) + r.completion_tokens
    u["cost_usd"] = round(u.get("cost_usd", 0.0) + r.cost_usd, 6)
    models = list(u.get("models", []))
    if r.model and r.model not in models:
        models.append(r.model)
    u["models"] = models
    return u


def parse_json_obj(text: str) -> dict[str, Any] | None:
    """Robustly pull the first JSON object out of a model reply (handles ```json fences / chatter)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, dict) else None
    except json.JSONDecodeError:
        return None


# ------------------------------------- nodes -----------------------------------------


async def plan_node(state: AgentState, config: RunnableConfig) -> AgentState:
    d = _deps(config)
    history = [m for m in state["messages"] if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str)][-6:]
    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in history)
    r = await d.llm.complete(
        [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": f"Conversation:\n{convo}"}],
        role="planner",
    )
    data = parse_json_obj(r.content) or {}
    steps = [str(s)[:160] for s in data.get("steps", []) if str(s).strip()][:5] or ["Work out the answer"]
    needs = bool(data.get("needs_tools", True))
    await d.emit({"type": "plan", "steps": steps, "needs_tools": needs})
    return {"plan": steps, "needs_tools": needs, "usage": _add_usage(state.get("usage", {}), r)}


async def agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    d, s = _deps(config), _deps(config).settings
    if await d.is_cancelled():
        raise RunCancelled()
    usage = state.get("usage", {})
    force = state.get("force_answer", False)
    stop_reason = state.get("stop_reason")
    if usage.get("cost_usd", 0) >= s.max_run_cost_usd and not force:
        force, stop_reason = True, "cost_budget"
    messages = list(state["messages"])
    if force:
        messages.append(
            {
                "role": "user",
                "content": "[system] Tool budget exhausted. Write the best final answer now from the evidence above; note any gaps.",
            }
        )
    use_tools = state.get("needs_tools", True) and not force

    step = state.get("steps", 0) + 1
    started = time.perf_counter()
    first_token: list[float] = []

    async def on_token(t: str) -> None:
        if not first_token:
            first_token.append(time.perf_counter() - started)
        await d.emit({"type": "token", "text": t})

    await d.emit({"type": "step.started", "step": step, "phase": "answering" if not use_tools else "thinking"})
    r = await d.llm.complete(messages, role="main", tools=d.tools.schemas() if use_tools else None, on_token=on_token)
    assistant: dict[str, Any] = {"role": "assistant", "content": r.content or ""}
    if r.tool_calls:
        assistant["tool_calls"] = [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments or "{}"}} for tc in r.tool_calls
        ]
        if r.content:
            await d.emit({"type": "answer.reset", "reason": "tool_call", "note": r.content[:2000]})
    out: AgentState = {
        "messages": [*state["messages"], assistant],
        "usage": _add_usage(usage, r),
        "steps": step,
        "draft": "" if r.tool_calls else r.content,
        "force_answer": force,
    }
    if stop_reason:
        out["stop_reason"] = stop_reason
    if first_token:
        out.setdefault("usage", {})["ttft_s"] = out["usage"].get("ttft_s") or round(first_token[0], 3)
    return out


def route_after_agent(state: AgentState, config: RunnableConfig) -> str:
    s = _deps(config).settings
    last = state["messages"][-1]
    if last.get("tool_calls"):
        return "tools"
    if s.enable_reflection and state.get("needs_tools") and state.get("revisions", 0) == 0 and not state.get("force_answer"):
        return "reflect"
    return "finalize"


async def tools_node(state: AgentState, config: RunnableConfig) -> AgentState:
    d, s = _deps(config), _deps(config).settings
    calls = state["messages"][-1]["tool_calls"]
    used = state.get("tool_calls", 0)
    allowed = max(0, s.max_tool_calls_per_run - used)

    async def run_one(i: int, call: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            args = {}
        await d.emit({"type": "tool.call", "id": call["id"], "tool": name, "args": args})
        if i >= allowed:
            text = f"Error: tool-call budget ({s.max_tool_calls_per_run}) exhausted for this run."
            await d.emit({"type": "tool.result", "id": call["id"], "tool": name, "ok": False, "preview": text, "ms": 0})
            return {"role": "tool", "tool_call_id": call["id"], "content": text}, None
        t0 = time.perf_counter()
        out, model_text, flags = await d.tools.execute(name, args, d.ctx)
        ms = int((time.perf_counter() - t0) * 1000)
        if flags:
            await d.emit({"type": "guard.flag", "tool": name, "flags": flags})
        await d.emit(
            {
                "type": "tool.result",
                "id": call["id"],
                "tool": name,
                "ok": not out.error,
                "ms": ms,
                "preview": out.text[:1500],
                "images": out.images,
                "sources": out.sources,
            }
        )
        return {"role": "tool", "tool_call_id": call["id"], "content": model_text or out.text}, out

    results = await asyncio.gather(*(run_one(i, c) for i, c in enumerate(calls)))
    images = list(state.get("images", []))
    sources = list(state.get("sources", []))
    for _, out in results:
        if out is not None:
            images.extend(out.images)
            sources.extend(x for x in out.sources if x.get("url") and x not in sources)
    total = used + len(calls)
    out_state: AgentState = {
        "messages": [*state["messages"], *(m for m, _ in results)],
        "tool_calls": total,
        "images": images[-8:],
        "sources": sources[:20],
    }
    if state.get("steps", 0) >= s.max_agent_steps or total >= s.max_tool_calls_per_run:
        out_state["force_answer"] = True
        out_state["stop_reason"] = "step_budget"
    return out_state


async def reflect_node(state: AgentState, config: RunnableConfig) -> AgentState:
    d = _deps(config)
    evidence = "\n---\n".join(str(m["content"])[:1200] for m in state["messages"] if m["role"] == "tool")[-6000:]
    r = await d.llm.complete(
        [
            {"role": "system", "content": CRITIC_PROMPT},
            {
                "role": "user",
                "content": f"USER REQUEST:\n{state['query']}\n\nEVIDENCE:\n{evidence or '(none)'}\n\nDRAFT:\n{state.get('draft', '')}",
            },
        ],
        role="critic",
    )
    data = parse_json_obj(r.content) or {}
    try:
        score = int(data.get("score", 10))
    except (TypeError, ValueError):
        score = 10
    feedback = str(data.get("feedback", ""))[:600]
    revise = score < 7 and bool(feedback)
    await d.emit({"type": "reflection", "score": score, "feedback": feedback, "revise": revise})
    out: AgentState = {"usage": _add_usage(state.get("usage", {}), r), "revisions": state.get("revisions", 0) + 1}
    if revise:
        await d.emit({"type": "answer.reset", "reason": "revision"})
        out["messages"] = [
            *state["messages"],
            {
                "role": "user",
                "content": f"[self-review] Your draft scored {score}/10. Fix this and write the complete improved answer: {feedback}",
            },
        ]
    return out


def route_after_reflect(state: AgentState) -> str:
    return "agent" if state["messages"][-1]["role"] == "user" else "finalize"


async def finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    return {
        "final": state.get("draft", "").strip() or "I wasn't able to produce an answer.",
        "stop_reason": state.get("stop_reason") or "completed",
    }


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("reflect", reflect_node)
    g.add_node("finalize", finalize_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "agent")
    g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "reflect": "reflect", "finalize": "finalize"})
    g.add_edge("tools", "agent")
    g.add_conditional_edges("reflect", route_after_reflect, {"agent": "agent", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


async def run_agent(history: list[dict[str, Any]], query: str, deps: AgentDeps) -> AgentState:
    messages = [{"role": "system", "content": system_prompt(deps.tools.names())}, *history, {"role": "user", "content": query}]
    initial: AgentState = {
        "messages": messages,
        "query": query,
        "steps": 0,
        "tool_calls": 0,
        "revisions": 0,
        "usage": {},
        "images": [],
        "sources": [],
    }
    limit = 4 * deps.settings.max_agent_steps + 10
    return await GRAPH.ainvoke(initial, config={"configurable": {"deps": deps}, "recursion_limit": limit})
