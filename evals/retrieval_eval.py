"""Retrieval evaluation: recall@k, MRR and nDCG@5 for each retrieval configuration.

Indexes the bundled Northwind knowledge base (backend/app/samples/*.md) in-process, runs the labelled
queries in evals/retrieval_qrels.jsonl and scores every configuration. A chunk is relevant when it contains
the query's answer span, so labels survive changes to chunk size.

    python evals/retrieval_eval.py                                     # offline: local embeddings
    EMBEDDING_MODEL=gemini/gemini-embedding-001 python evals/retrieval_eval.py   # real embeddings
    python evals/retrieval_eval.py --rerank llm                        # also score LLM reranking (needs a model key)

Writes evals/results/retrieval.md and retrieval.json; exits non-zero if hybrid recall@5 < --min-recall.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
BACKEND = ROOT.parent / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/retrieval.db")
os.environ.setdefault("ENVIRONMENT", "test")


def _relevant(hit: dict, answer: str) -> bool:
    norm = lambda s: " ".join(s.lower().split())  # noqa: E731
    return norm(answer) in norm(hit["text"])


def _metrics(ranks: list[int | None], ks=(1, 3, 5)) -> dict[str, float]:
    n = len(ranks) or 1
    out = {f"recall@{k}": sum(1 for r in ranks if r is not None and r <= k) / n for k in ks}
    out["mrr"] = sum(1 / r for r in ranks if r is not None) / n
    out["ndcg@5"] = sum(1 / math.log2(r + 1) for r in ranks if r is not None and r <= 5) / n  # one relevant chunk per query
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerank", default="none", help="none | llm | a LiteLLM rerank model (adds 'hybrid+rerank' row)")
    ap.add_argument("--min-recall", type=float, default=0.0)
    args = ap.parse_args()

    from app import db, rag
    from app.config import get_settings

    get_settings.cache_clear()
    await db.init_db()
    from app.models import User

    user = "eval-user"
    async with db.sessionmaker()() as session:
        if await session.get(User, user) is None:
            session.add(User(id=user, email="eval@example.com", password_hash="x"))
            await session.commit()
        for p in sorted((BACKEND / "app" / "samples").glob("*.md")):
            await rag.ingest(session, user, p.name, "text/markdown", p.read_bytes())

    qrels = [json.loads(line) for line in (ROOT / "retrieval_qrels.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    configs = [("vector", "vector", "none"), ("bm25", "lexical", "none"), ("hybrid (RRF)", "hybrid", "none")]
    if args.rerank != "none":
        configs.append((f"hybrid + rerank ({args.rerank})", "hybrid", args.rerank))

    table, per_type, details = {}, {}, {}
    async with db.sessionmaker()() as session:
        for label, mode, rr in configs:
            ranks, lat = [], []
            for q in qrels:
                t0 = time.perf_counter()
                hits = await rag.search(session, user, q["q"], k=10, mode=mode, rerank_method=rr)
                lat.append(time.perf_counter() - t0)
                ranks.append(next((i + 1 for i, h in enumerate(hits) if _relevant(h, q["answer"])), None))
            table[label] = {**_metrics(ranks), "p50_ms": sorted(lat)[len(lat) // 2] * 1000}
            per_type[label] = {
                t: _metrics([r for r, q in zip(ranks, qrels, strict=True) if q["type"] == t])["recall@5"]
                for t in sorted({q["type"] for q in qrels})
            }
            details[label] = [{"q": q["q"], "rank": r} for q, r in zip(qrels, ranks, strict=True)]
    await db.dispose_db()

    emb = os.environ.get("EMBEDDING_MODEL") or "local feature-hashing (offline)"
    types = sorted({q["type"] for q in qrels})
    mix = ", ".join(f"{sum(q['type'] == t for q in qrels)} {t}" for t in types)
    lines = [
        "# Retrieval eval",
        "",
        f"Corpus: Northwind knowledge base ({len(list((BACKEND / 'app' / 'samples').glob('*.md')))} docs). "
        f"Queries: {len(qrels)} ({mix}). Embeddings: {emb}.",
        "",
        "| Retriever | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 | p50 latency |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {k} | {v['recall@1']:.2f} | {v['recall@3']:.2f} | {v['recall@5']:.2f} | {v['mrr']:.3f} | {v['ndcg@5']:.3f} | {v['p50_ms']:.0f} ms |"
            for k, v in table.items()
        ],
        "",
        "Recall@5 by query type:",
        "",
        "| Retriever | " + " | ".join(types) + " |",
        "|---|" + "---|" * len(types),
        *[f"| {k} | " + " | ".join(f"{v[t]:.2f}" for t in types) + " |" for k, v in per_type.items()],
        "",
        "Misses (not in top 10) for the hybrid retriever:",
        "",
        *([f"- {d['q']}" for d in details["hybrid (RRF)"] if d["rank"] is None] or ["None."]),
    ]
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "retrieval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "retrieval.json").write_text(json.dumps({"metrics": table, "by_type": per_type, "details": details}, indent=2), encoding="utf-8")
    print("\n".join(lines))
    return 0 if table["hybrid (RRF)"]["recall@5"] >= args.min_recall else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
