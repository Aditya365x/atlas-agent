"""Black-box evaluation harness for Atlas.

Drives a running API exactly like a user would (register -> upload fixtures -> start runs -> stream SSE),
then scores each task with deterministic checks and, optionally, an LLM judge.

    python evals/run_evals.py --api http://localhost:8000              # full suite (live model)
    python evals/run_evals.py --api http://localhost:8000 --offline    # CI smoke suite (LLM_MODE=fake)
    python evals/run_evals.py --judge-model gemini/gemini-2.5-flash    # add LLM-as-judge scoring

Outputs evals/results/report.md + results.json; exits non-zero if the pass rate is below --min-pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
TERMINAL = {"run.completed", "run.failed", "run.cancelled"}


@dataclass
class Result:
    id: str
    category: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    ttft_s: float | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    tools: list[str] = field(default_factory=list)
    judge_score: float | None = None
    answer: str = ""


async def run_task(client: httpx.AsyncClient, headers: dict, task: dict, offline: bool, timeout_s: float) -> tuple[list[dict], float]:
    t0 = time.perf_counter()
    r = await client.post("/v1/runs", json={"message": task["prompt"]}, headers=headers)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    events: list[dict] = []

    async def consume():
        async with client.stream("GET", f"/v1/runs/{run_id}/events", headers=headers) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev = json.loads(line[5:].strip())
                    events.append(ev)
                    if ev["type"] in TERMINAL:
                        return

    await asyncio.wait_for(consume(), timeout=timeout_s)
    return events, time.perf_counter() - t0


def score(task: dict, events: list[dict], offline: bool) -> list[str]:
    checks = dict(task.get("checks", {}))
    if not offline:
        checks.update(checks.pop("live", {}))
    else:
        checks.pop("live", None)
    end = events[-1] if events else {}
    if end.get("type") != "run.completed":
        return [f"run ended with {end.get('type')}: {end.get('error', '')}"]
    answer = (end.get("answer") or "").lower()
    calls = [e["tool"] for e in events if e["type"] == "tool.call"]
    results = [e for e in events if e["type"] == "tool.result"]
    fails = []
    if (opts := checks.get("answer_contains_any")) and not any(o.lower() in answer for o in opts):
        fails.append(f"answer lacks any of {opts}")
    for bad in checks.get("answer_not_contains", []):
        if bad.lower() in answer:
            fails.append(f"answer contains forbidden '{bad}'")
    for key in ("must_use_tool", "also_tool"):
        if (tool := checks.get(key)) and tool not in calls:
            fails.append(f"did not use {tool} (used {calls or 'none'})")
    if "max_tool_calls" in checks and len(calls) > checks["max_tool_calls"]:
        fails.append(f"used {len(calls)} tools, expected <= {checks['max_tool_calls']}")
    if (n := checks.get("min_images")) and sum(len(e.get("images") or []) for e in results) < n:
        fails.append(f"expected >= {n} image(s)")
    if (n := checks.get("min_sources")) and len({s["url"] for e in results for s in e.get("sources") or [] if s.get("url")}) < n:
        fails.append(f"expected >= {n} source(s)")
    if checks.get("guard_flag") and not any(e["type"] == "guard.flag" for e in events):
        fails.append("prompt-injection guard did not fire")
    if (tool := checks.get("tool_failed")) and calls and not any(e["tool"] == tool and not e["ok"] for e in results):
        fails.append(f"{tool} should have been refused")
    return fails


async def judge(model: str, task: dict, answer: str) -> float | None:
    try:
        import litellm
    except ImportError:
        return None
    prompt = (
        "Grade the assistant answer for the user request on a 1-10 scale for correctness, completeness and "
        f'helpfulness. Reply with JSON {{"score": n}} only.\n\nREQUEST:\n{task["prompt"]}\n\nANSWER:\n{answer[:6000]}'
    )
    try:
        resp = await litellm.acompletion(model=model, messages=[{"role": "user", "content": prompt}], temperature=0)
        text = resp.choices[0].message.content or ""
        return float(json.loads(text[text.index("{") : text.rindex("}") + 1])["score"])
    except Exception:
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--offline", action="store_true", help="only tasks the deterministic fake model can pass")
    ap.add_argument("--only", help="comma-separated task ids or categories")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--min-pass", type=float, default=0.8)
    ap.add_argument("--judge-model")
    args = ap.parse_args()

    tasks = [json.loads(line) for line in (ROOT / "tasks.jsonl").read_text().splitlines() if line.strip()]
    if args.offline:
        tasks = [t for t in tasks if t.get("offline")]
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep or t["category"] in keep]

    async with httpx.AsyncClient(base_url=args.api, timeout=60) as client:
        reg = await client.post("/v1/auth/register", json={"email": f"eval-{uuid.uuid4().hex[:8]}@atlas.dev", "password": "eval-password-1"})
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        for fx in sorted({t["fixture"] for t in tasks if t.get("fixture")}):
            path = ROOT / "fixtures" / fx
            data = (path if path.exists() else ROOT.parent / "backend" / "app" / "samples" / fx).read_bytes()
            (await client.post("/v1/files", files={"file": (fx, data)}, headers=headers)).raise_for_status()

        sem = asyncio.Semaphore(args.concurrency)

        async def one(task: dict) -> Result:
            async with sem:
                try:
                    events, latency = await run_task(client, headers, task, args.offline, args.timeout)
                except Exception as e:  # noqa: BLE001
                    return Result(task["id"], task["category"], False, [f"harness error: {type(e).__name__}: {e}"])
            fails = score(task, events, args.offline)
            end = events[-1] if events else {}
            usage = end.get("usage") or {}
            res = Result(
                task["id"], task["category"], not fails, fails, round(latency, 2), usage.get("ttft_s"),
                usage.get("cost_usd", 0.0), usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
                [e["tool"] for e in events if e["type"] == "tool.call"], None, (end.get("answer") or "")[:2000],
            )
            if args.judge_model and end.get("type") == "run.completed":
                res.judge_score = await judge(args.judge_model, task, res.answer)
            print(f"{'PASS' if res.passed else 'FAIL'}  {task['id']:<28} {res.latency_s:>6.1f}s  {', '.join(fails)}", flush=True)
            return res

        results = await asyncio.gather(*(one(t) for t in tasks))

    write_report(results, args)
    rate = sum(r.passed for r in results) / max(len(results), 1)
    print(f"\npass rate {rate:.0%} ({sum(r.passed for r in results)}/{len(results)})  ->  evals/results/report.md")
    return 0 if rate >= args.min_pass else 1


def write_report(results: list[Result], args) -> None:
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    lat = sorted(r.latency_s for r in results if r.passed)
    pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0  # noqa: E731
    ttft = [r.ttft_s for r in results if r.ttft_s]
    cats: dict[str, list[Result]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)
    judged = [r.judge_score for r in results if r.judge_score is not None]
    lines = [
        "# Atlas eval report",
        "",
        f"Mode: {'offline (fake model)' if args.offline else 'live'}. Tasks: {len(results)}.",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Pass rate | {sum(r.passed for r in results) / max(len(results), 1):.0%} |",
        f"| Latency p50 / p95 | {pct(0.5):.1f}s / {pct(0.95):.1f}s |",
        f"| Median time to first token | {statistics.median(ttft):.2f}s |" if ttft else "| Median time to first token | n/a |",
        f"| Avg cost per task | ${statistics.mean([r.cost_usd for r in results]):.4f} |" if results else "",
        f"| Avg tokens per task | {statistics.mean([r.tokens for r in results]):.0f} |" if results else "",
        f"| LLM-judge mean score | {statistics.mean(judged):.1f}/10 |" if judged else "",
        "",
        "| Category | Passed |", "|---|---|",
        *[f"| {c} | {sum(r.passed for r in rs)}/{len(rs)} |" for c, rs in sorted(cats.items())],
        "",
        "## Failures",
        *([f"- `{r.id}`: {'; '.join(r.failures)}" for r in results if not r.passed] or ["None."]),
    ]
    (out / "report.md").write_text("\n".join(x for x in lines if x is not None) + "\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
