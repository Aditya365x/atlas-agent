"""Async database engine, session dependency and schema bootstrap (incl. optional pgvector)."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import log
from app.models import Base

EMBED_DIM = 384  # local hashing embeddings + most small embedding models fit; configurable via migration

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
pgvector_enabled = False


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        url = s.database_url
        kwargs = (
            {}
            if url.startswith("sqlite")
            else {"pool_size": s.db_pool_size, "max_overflow": s.db_max_overflow, "pool_pre_ping": True, "pool_timeout": 20}
        )
        _engine = create_async_engine(url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


async def init_db() -> None:
    """Create tables and, on Postgres, enable pgvector + an HNSW index for ANN search."""
    global pgvector_enabled
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:  # additive columns for databases created by an older version
        await conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes integer DEFAULT 0"))
        await conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS raw bytea"))
        await conn.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS effort varchar(10) DEFAULT 'balanced'"))
        # full-text index for the lexical half of hybrid retrieval
        await conn.execute(text("CREATE INDEX IF NOT EXISTS chunks_fts ON chunks USING gin (to_tsvector('english', text))"))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS emb vector({EMBED_DIM})"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS chunks_emb_hnsw ON chunks USING hnsw (emb vector_cosine_ops)"))
        pgvector_enabled = True
        log.info("pgvector.enabled", dim=EMBED_DIM)
    except Exception as e:  # extension not installed -> exact cosine search in Python
        log.warning("pgvector.unavailable", error=str(e)[:200])


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine, _sessionmaker = None, None
