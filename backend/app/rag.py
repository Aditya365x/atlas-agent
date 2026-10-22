"""Per-user document RAG: extraction -> chunking -> embeddings -> hybrid retrieval -> optional rerank.

Retrieval runs two retrievers and fuses them with Reciprocal Rank Fusion (RRF):
  * dense: cosine similarity (pgvector HNSW on Postgres, exact in Python elsewhere)
  * lexical: BM25 (Postgres full-text search on Postgres, an in-process Okapi BM25 elsewhere)
RRF needs no score calibration between the two, and the lexical side rescues exact identifiers
(error codes, SKUs, names) that embeddings blur. A reranker (LLM listwise, or a hosted cross-encoder
such as Cohere/Jina via LiteLLM) can then reorder the fused shortlist.
`evals/retrieval_eval.py` measures recall@k / MRR / nDCG for each configuration.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import get_settings
from app.logging import log
from app.models import Chunk, Document
from app.tools.base import Tool, ToolContext, ToolOutput

CHUNK_CHARS = 1200
OVERLAP = 200
RRF_K = 60
_TOKEN = re.compile(r"[a-z0-9]+")
SUPPORTED = {".pdf", ".txt", ".md", ".csv", ".json", ".py", ".html", ".log", ".xlsx"}
DATA_FILES = {".csv", ".json", ".txt", ".md", ".log", ".py", ".xlsx"}  # also mounted into python_exec
_STOP = set(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was what when where which who why "
    "with my me do does did can you your we our they their there about into than then them so if not no".split()
)


# ---------------------------------- extraction / chunking ----------------------------


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    if name.endswith(".xlsx"):
        import pandas as pd

        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        return "\n\n".join(f"[sheet {n}]\n{df.to_csv(index=False)}" for n, df in sheets.items())
    if name.endswith(".html"):
        from app.tools.web import html_to_text

        return html_to_text(data.decode("utf-8", errors="replace"))[1]
    return data.decode("utf-8", errors="replace")


def chunk_text(text_: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text_) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # hard-split giant paragraphs
            head, p = p[:size], p[size - overlap :]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            cur = (cur[-overlap:] + "\n\n" + p) if cur and overlap else p
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------- embeddings ---------------------------------------


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def _terms(s: str) -> list[str]:
    return [t for t in _tokens(s) if t not in _STOP]


def hash_embed(s: str, dim: int = db.EMBED_DIM) -> list[float]:
    """Dependency-free embedding (signed feature hashing of unigrams + bigrams). Good enough for
    offline demos/tests; set EMBEDDING_MODEL for semantic quality in production."""
    vec = [0.0] * dim
    toks = _tokens(s)
    for feat in toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]:
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    return _normalize(vec)


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _fit_dim(v: list[float], dim: int = db.EMBED_DIM) -> list[float]:
    return _normalize((v + [0.0] * dim)[:dim])  # Matryoshka-style truncation for modern embedding models


async def embed(texts: list[str]) -> list[list[float]]:
    model = get_settings().embedding_model
    if not model:
        return [hash_embed(t) for t in texts]
    import litellm

    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        resp = await litellm.aembedding(model=model, input=texts[i : i + 64], dimensions=db.EMBED_DIM)
        out.extend(_fit_dim(d["embedding"]) for d in resp.data)
    return out


# ---------------------------------- ingest -------------------------------------------


async def ingest(session: AsyncSession, user_id: str, filename: str, content_type: str, data: bytes) -> Document:
    text_ = extract_text(filename, data)
    pieces = chunk_text(text_)
    vectors = await embed(pieces) if pieces else []
    keep_raw = Path(filename).suffix.lower() in DATA_FILES and len(data) <= get_settings().max_data_file_mb * 1024 * 1024
    doc = Document(
        user_id=user_id,
        filename=filename,
        content_type=content_type,
        num_chunks=len(pieces),
        size_bytes=len(data),
        raw=data if keep_raw else None,
    )
    session.add(doc)
    await session.flush()
    rows = [
        Chunk(document_id=doc.id, user_id=user_id, idx=i, text=p, embedding=v) for i, (p, v) in enumerate(zip(pieces, vectors, strict=True))
    ]
    session.add_all(rows)
    await session.flush()
    if db.pgvector_enabled and rows:
        for r in rows:
            await session.execute(text("UPDATE chunks SET emb = CAST(:e AS vector) WHERE id = :id"), {"e": str(r.embedding), "id": r.id})
    await session.commit()
    return doc


async def data_files(session: AsyncSession, user_id: str, max_total_mb: int = 25) -> dict[str, bytes]:
    """The user's raw data files (newest first, size-capped) for mounting into the code sandbox."""
    rows = await session.execute(
        select(Document.filename, Document.raw)
        .where(Document.user_id == user_id, Document.raw.is_not(None))
        .order_by(Document.created_at.desc())
        .limit(20)
    )
    out: dict[str, bytes] = {}
    budget = max_total_mb * 1024 * 1024
    for name, raw in rows:
        if name in out or raw is None or len(raw) > budget:
            continue
        out[name] = raw
        budget -= len(raw)
    return out


# ---------------------------------- retrieval ----------------------------------------


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 of `query` against each doc, with IDF computed over `docs`."""
    q = set(_terms(query))
    if not q or not docs:
        return [0.0] * len(docs)
    tfs = [Counter(_terms(d)) for d in docs]
    lens = [sum(tf.values()) for tf in tfs]
    avg = (sum(lens) / len(lens)) or 1.0
    n = len(docs)
    df = {t: sum(1 for tf in tfs if t in tf) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    return [
        sum(idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * ln / avg)) for t in q if tf[t]) for tf, ln in zip(tfs, lens, strict=True)
    ]


def rrf(*rankings: list[str], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank)."""
    out: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            out[key] = out.get(key, 0.0) + 1.0 / (k + rank)
    return out


async def _candidates(session: AsyncSession, user_id: str, query: str, pool: int) -> tuple[dict[str, dict], list[str], list[str]]:
    """Returns (chunks by id, dense ranking, lexical ranking)."""
    qv = (await embed([query]))[0]
    chunks: dict[str, dict] = {}
    if db.pgvector_enabled:
        dense = await session.execute(
            text(
                "SELECT c.id, c.text, c.idx, d.filename FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.user_id = :u AND c.emb IS NOT NULL ORDER BY c.emb <=> CAST(:q AS vector) LIMIT :n"
            ),
            {"q": str(qv), "u": user_id, "n": pool},
        )
        dense_rank = []
        for r in dense:
            chunks[r.id] = {"text": r.text, "idx": r.idx, "filename": r.filename}
            dense_rank.append(r.id)
        lex_rank = []
        if terms := sorted(set(_terms(query))):
            lex = await session.execute(
                text(
                    "SELECT c.id, c.text, c.idx, d.filename FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE c.user_id = :u AND to_tsvector('english', c.text) @@ to_tsquery('english', :tq) "
                    "ORDER BY ts_rank_cd(to_tsvector('english', c.text), to_tsquery('english', :tq)) DESC LIMIT :n"
                ),
                {"tq": " | ".join(terms), "u": user_id, "n": pool},
            )
            for r in lex:
                chunks.setdefault(r.id, {"text": r.text, "idx": r.idx, "filename": r.filename})
                lex_rank.append(r.id)
        return chunks, dense_rank, lex_rank

    res = await session.execute(
        select(Chunk.id, Chunk.text, Chunk.idx, Chunk.embedding, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.user_id == user_id)
        .limit(20_000)
    )
    sims: dict[str, float] = {}
    for cid, t, i, e, f in res:
        chunks[cid] = {"text": t, "idx": i, "filename": f}
        sims[cid] = sum(a * b for a, b in zip(qv, e, strict=False))
    dense_rank = sorted(sims, key=sims.get, reverse=True)[:pool]
    ids = list(chunks)
    bm = bm25_scores(query, [chunks[c]["text"] for c in ids])
    lex_rank = [c for c, sc in sorted(zip(ids, bm, strict=True), key=lambda x: x[1], reverse=True) if sc > 0][:pool]
    return chunks, dense_rank, lex_rank


async def rerank(query: str, hits: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    """Reorder a shortlist. Falls back to the input order on any failure: reranking must never break search."""
    if method == "none" or len(hits) < 2:
        return hits
    try:
        if method == "llm":
            from app.agent.graph import parse_json_obj
            from app.llm import get_llm

            listing = "\n\n".join(f"[{i}] {h['text'][:600]}" for i, h in enumerate(hits))
            r = await get_llm().complete(
                [
                    {
                        "role": "system",
                        "content": 'You rank passages by how well they answer a query. Reply with ONLY JSON: {"ranking": [passage numbers, most relevant first]}',
                    },
                    {"role": "user", "content": f"QUERY: {query}\n\nPASSAGES:\n{listing}"},
                ],
                role="planner",
            )
            order = [int(i) for i in (parse_json_obj(r.content) or {}).get("ranking", []) if str(i).isdigit()]
        else:
            import litellm

            resp = await litellm.arerank(model=method, query=query, documents=[h["text"] for h in hits], top_n=len(hits))
            order = [int(x["index"]) for x in resp.results]
        picked = [i for i in dict.fromkeys(order) if 0 <= i < len(hits)]
        if not picked:
            return hits
        return [hits[i] for i in picked] + [h for i, h in enumerate(hits) if i not in picked]
    except Exception as e:  # noqa: BLE001
        log.warning("rerank.failed", method=method, error=str(e)[:200])
        return hits


async def search(
    session: AsyncSession, user_id: str, query: str, k: int = 5, mode: str | None = None, rerank_method: str | None = None
) -> list[dict[str, Any]]:
    s = get_settings()
    mode = mode or s.retrieval_mode
    rerank_method = rerank_method or s.rerank
    chunks, dense, lexical = await _candidates(session, user_id, query, pool=max(40, 4 * k))
    fused = rrf(*{"vector": [dense], "lexical": [lexical], "hybrid": [dense, lexical]}[mode])
    order = sorted(fused, key=fused.get, reverse=True)
    shortlist = [{**chunks[c], "id": c, "score": round(fused[c], 5)} for c in order[: max(k, 20) if rerank_method != "none" else k]]
    return (await rerank(query, shortlist, rerank_method))[:k]


async def file_search(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolOutput("Error: empty query", error=True)
    async with db.sessionmaker()() as session:
        hits = await search(session, ctx.user_id, query, k=int(args.get("k", 5) or 5))
    if not hits:
        return ToolOutput("No uploaded documents matched. The user may not have uploaded any files.")
    body = "\n\n".join(f"[{h['filename']} #chunk{h['idx']} score={h['score']}]\n{h['text']}" for h in hits)
    sources = [{"title": f"{h['filename']} (chunk {h['idx']})", "url": ""} for h in hits]
    return ToolOutput(body, sources=sources, untrusted=True)


TOOL = Tool(
    name="file_search",
    description=(
        "Hybrid semantic + keyword (BM25) search over documents the user uploaded (PDF, txt, md, csv, xlsx...). "
        "Returns the most relevant passages with filenames. For numeric analysis of a CSV/XLSX, use python_exec instead."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer", "description": "passages to return, default 5"}},
        "required": ["query"],
    },
    fn=file_search,
    timeout_s=30,
)
