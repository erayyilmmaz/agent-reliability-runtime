"""A shared atomic fixed-window limiter keyed by the authenticated principal."""

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
    """Increment, expiry (including orphan repair), and TTL are one Redis operation."""

    _SCRIPT = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {current, ttl}
"""

    def __init__(self, *, redis_url: str, limit: int, window_seconds: int) -> None:
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._limit = limit
        self._window_seconds = window_seconds

    async def check(self, client_id: str) -> RateLimitDecision:
        key = f"arr:rate-limit:{hashlib.sha256(client_id.encode('utf-8')).hexdigest()}"
        try:
            current, ttl = await self._client.eval(  # type: ignore[no-untyped-call]
                self._SCRIPT, 1, key, self._window_seconds
            )
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
