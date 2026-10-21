"""Infrastructure adapters with Redis-backed and in-memory implementations.

- EventBus: durable, replayable run event streams (Redis Streams) so clients can reconnect
  mid-run with Last-Event-ID and never miss a token.
- RateLimiter: sliding-window limiter shared across API replicas.
- CancelFlags: cooperative cancellation of in-flight runs from any replica.
"""

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
STREAM_TTL_S = 24 * 3600


class EventBus:
    async def publish(self, run_id: str, event: dict[str, Any]) -> str: ...
    def subscribe(self, run_id: str, last_id: str | None = None) -> AsyncIterator[tuple[str, dict]]: ...


class MemoryEventBus(EventBus):
    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = defaultdict(list)
        self._cond: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def publish(self, run_id: str, event: dict[str, Any]) -> str:
        cond = self._cond[run_id]
        async with cond:
            self._events[run_id].append(event)
            cond.notify_all()
            return str(len(self._events[run_id]) - 1)

    async def subscribe(self, run_id: str, last_id: str | None = None) -> AsyncIterator[tuple[str, dict]]:
        idx = int(last_id) + 1 if last_id is not None and last_id.isdigit() else 0
        cond = self._cond[run_id]
        while True:
            async with cond:
                while idx >= len(self._events[run_id]):
                    await asyncio.wait_for(cond.wait(), timeout=300)
                batch = self._events[run_id][idx:]
            for ev in batch:
                yield str(idx), ev
                idx += 1
                if ev["type"] in TERMINAL_EVENTS:
                    return


class RedisEventBus(EventBus):
    def __init__(self, client: aioredis.Redis) -> None:
        self.r = client

    async def publish(self, run_id: str, event: dict[str, Any]) -> str:
        key = f"run:{run_id}:events"
        eid = await self.r.xadd(key, {"d": json.dumps(event, default=str)}, maxlen=20_000, approximate=True)
        await self.r.expire(key, STREAM_TTL_S)
        return eid.decode() if isinstance(eid, bytes) else eid

    async def subscribe(self, run_id: str, last_id: str | None = None) -> AsyncIterator[tuple[str, dict]]:
        key = f"run:{run_id}:events"
        cursor = last_id or "0-0"
        while True:
            resp = await self.r.xread({key: cursor}, block=15_000, count=200)
            for _, entries in resp or []:
                for eid, fields in entries:
                    cursor = eid.decode() if isinstance(eid, bytes) else eid
                    raw = fields.get(b"d") or fields.get("d")
                    ev = json.loads(raw)
                    yield cursor, ev
                    if ev["type"] in TERMINAL_EVENTS:
                        return


class RateLimiter:
    """Sliding-window log limiter. Returns (allowed, retry_after_seconds)."""

    def __init__(self, client: aioredis.Redis | None) -> None:
        self.r = client
        self._mem: dict[str, list[float]] = defaultdict(list)

    async def hit(self, key: str, limit: int, window_s: int = 60) -> tuple[bool, int]:
        now = time.time()
        if self.r is None:
            bucket = [t for t in self._mem[key] if t > now - window_s]
            self._mem[key] = bucket
            if len(bucket) >= limit:
                return False, int(bucket[0] + window_s - now) + 1
            bucket.append(now)
            return True, 0
        rkey = f"rl:{key}"
        pipe = self.r.pipeline()
        pipe.zremrangebyscore(rkey, 0, now - window_s)
        pipe.zcard(rkey)
        pipe.zrange(rkey, 0, 0, withscores=True)
        _, count, oldest = await pipe.execute()
        if count >= limit:
            retry = int(oldest[0][1] + window_s - now) + 1 if oldest else window_s
            return False, retry
        await self.r.zadd(rkey, {f"{now}:{uuid.uuid4().hex[:6]}": now})
        await self.r.expire(rkey, window_s)
        return True, 0


class CancelFlags:
    def __init__(self, client: aioredis.Redis | None) -> None:
        self.r = client
        self._mem: set[str] = set()

    async def cancel(self, run_id: str) -> None:
        if self.r is None:
            self._mem.add(run_id)
        else:
            await self.r.set(f"run:{run_id}:cancel", "1", ex=3600)

    async def is_cancelled(self, run_id: str) -> bool:
        if self.r is None:
            return run_id in self._mem
        return bool(await self.r.exists(f"run:{run_id}:cancel"))


class Infra:
    def __init__(self) -> None:
        url = get_settings().redis_url
        self.redis: aioredis.Redis | None = aioredis.from_url(url) if url else None
        self.bus: EventBus = RedisEventBus(self.redis) if self.redis else MemoryEventBus()
        self.limiter = RateLimiter(self.redis)
        self.cancel = CancelFlags(self.redis)

    async def ping(self) -> bool:
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()


_infra: Infra | None = None


def get_infra() -> Infra:
    global _infra
    if _infra is None:
        _infra = Infra()
    return _infra


def reset_infra() -> None:
    global _infra
    _infra = None
