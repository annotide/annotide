"""Request rate limits per user, per API key and per login target (API-5).

Fixed one-minute windows: `INCR ratelimit:{scope}:{id}:{window}` and an
expiry on the first hit. Coarser than a sliding window — a burst straddling
a boundary can reach twice the limit — but one round trip, no Lua, and
plenty to stop a runaway script or a password guesser.

The limiter fails open: when Redis is unreachable the request goes through
and a warning is logged. Refusing every request because the counter store
is down would turn a Redis blip into a full outage.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.core.redis import redis_client

log = get_logger(__name__)

WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the current window ends (the `Retry-After` value).
    reset_after: int


class RateLimiter(Protocol):
    async def hit(self, scope: str, identity: str, limit: int) -> Decision: ...

    async def aclose(self) -> None: ...


def _window(now: float) -> tuple[int, int]:
    window = int(now // WINDOW_SECONDS)
    reset_after = max(1, (window + 1) * WINDOW_SECONDS - int(now))
    return window, reset_after


def _decision(count: int, limit: int, reset_after: int) -> Decision:
    return Decision(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_after=reset_after,
    )


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, scope: str, identity: str, limit: int) -> Decision:
        window, reset_after = _window(time.time())
        key = f"ratelimit:{scope}:{identity}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, WINDOW_SECONDS * 2, nx=True)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            log.warning("rate_limit.unavailable", scope=scope, error=str(exc))
            return Decision(allowed=True, limit=limit, remaining=limit, reset_after=reset_after)
        return _decision(int(count), limit, reset_after)

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemoryRateLimiter:
    """In-process counters, for tests and single-process tooling."""

    def __init__(self, now: float | None = None) -> None:
        self._counts: dict[str, int] = {}
        self.now = now

    async def hit(self, scope: str, identity: str, limit: int) -> Decision:
        window, reset_after = _window(time.time() if self.now is None else self.now)
        key = f"{scope}:{identity}:{window}"
        self._counts[key] = self._counts.get(key, 0) + 1
        return _decision(self._counts[key], limit, reset_after)

    async def aclose(self) -> None:
        self._counts.clear()


def login_identity(client_ip: str | None, email: str) -> str:
    """The login counter's key: IP plus a hash of the e-mail, never the e-mail itself."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]
    return f"{client_ip or 'unknown'}:{digest}"


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        # Short timeouts: a hung Redis must fail open quickly, not stall requests.
        _limiter = RedisRateLimiter(redis_client(socket_connect_timeout=0.5, socket_timeout=0.5))
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Install a limiter (tests), or `None` to build the Redis one on next use."""
    global _limiter
    _limiter = limiter


async def aclose_rate_limiter() -> None:
    global _limiter
    if _limiter is not None:
        await _limiter.aclose()
        _limiter = None
