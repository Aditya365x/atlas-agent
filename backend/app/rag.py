"""Per-user document RAG: extraction -> chunking -> embeddings -> hybrid (vector + lexical) retrieval.

On Postgres+pgvector an HNSW index pre-selects candidates; elsewhere exact cosine runs in Python.
Results are always re-ranked with a lexical score so exact identifiers (error codes, names) are not lost.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.config import get_settings
from app.models import Chunk, Document
from app.tools.base import Tool, ToolContext, ToolOutput

CHUNK_CHARS = 1200
OVERLAP = 200
_TOKEN = re.compile(r"[a-z0-9]+")
SUPPORTED = {".pdf", ".txt", ".md", ".csv", ".json", ".py", ".html", ".log"}


# ---------------------------------- extraction / chunking ----------------------------


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
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


# ---------------------------------- ingest / search ----------------------------------


async def ingest(session: AsyncSession, user_id: str, filename: str, content_type: str, data: bytes) -> Document:
    text_ = extract_text(filename, data)
    pieces = chunk_text(text_)
    vectors = await embed(pieces) if pieces else []
    doc = Document(user_id=user_id, filename=filename, content_type=content_type, num_chunks=len(pieces))
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


def _lexical(query: str, passage: str) -> float:
    q = Counter(_tokens(query))
    if not q:
        return 0.0
    p = Counter(_tokens(passage))
    hits = sum(min(c, p[t]) for t, c in q.items())
    return hits / sum(q.values())


async def search(session: AsyncSession, user_id: str, query: str, k: int = 5) -> list[dict[str, Any]]:
    qv = (await embed([query]))[0]
    if db.pgvector_enabled:
        res = await session.execute(
            text(
                "SELECT c.id, c.text, c.idx, d.filename, 1 - (c.emb <=> CAST(:q AS vector)) AS sim "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.user_id = :u AND c.emb IS NOT NULL ORDER BY c.emb <=> CAST(:q AS vector) LIMIT 40"
            ),
            {"q": str(qv), "u": user_id},
        )
        cands = [{"text": r.text, "idx": r.idx, "filename": r.filename, "vec": float(r.sim)} for r in res]
    else:
        res = await session.execute(
            select(Chunk.text, Chunk.idx, Chunk.embedding, Document.filename)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.user_id == user_id)
            .limit(20_000)
        )
        cands = [{"text": t, "idx": i, "filename": f, "vec": sum(a * b for a, b in zip(qv, e, strict=False))} for t, i, e, f in res]
    for c in cands:
        c["score"] = round(0.7 * c["vec"] + 0.3 * _lexical(query, c["text"]), 4)
    return sorted(cands, key=lambda c: c["score"], reverse=True)[:k]


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
    description="Semantic + keyword search over documents the user uploaded (PDF, txt, md, csv...). Returns the most relevant passages with filenames.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer", "description": "passages to return, default 5"}},
        "required": ["query"],
    },
    fn=file_search,
    timeout_s=30,
)
