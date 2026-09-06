"""A Redis-backed shared fixed-window limiter keyed by a hashed client identifier."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as redis
from redis.exceptions import RedisError


class RateLimitUnavailable(RuntimeError):
    """The security boundary cannot contact its shared rate-limit store."""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def check(self, client_id: str) -> RateLimitDecision: ...

    async def close(self) -> None: ...


class RedisFixedWindowRateLimiter:
    """Uses Redis INCR/EXPIRE so every API instance observes the same limit."""

    def __init__(self, *, redis_url: str, limit: int, window_seconds: int) -> None:
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._limit = limit
        self._window_seconds = window_seconds

    async def check(self, client_id: str) -> RateLimitDecision:
        key = f"arr:rate-limit:{hashlib.sha256(client_id.encode('utf-8')).hexdigest()}"
        try:
            current = await self._client.incr(key)
            if current == 1:
                await self._client.expire(key, self._window_seconds)
            ttl = await self._client.ttl(key)
        except RedisError as exc:
            raise RateLimitUnavailable("Redis rate limiter is unavailable") from exc
        retry_after = max(1, ttl if ttl > 0 else self._window_seconds)
        return RateLimitDecision(allowed=current <= self._limit, retry_after_seconds=retry_after)

    async def close(self) -> None:
        await self._client.close()


class NoopRateLimiter:
    """Used only in explicitly disabled local-auth mode and narrow API tests."""

    async def check(self, client_id: str) -> RateLimitDecision:
        del client_id
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    async def close(self) -> None:
        return None
