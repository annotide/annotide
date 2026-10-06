"""Worker liveness check: `python -m app.worker.check`, exit 0 when healthy.

The same test as `arq --check`: the worker refreshes a health key in Redis
while it runs, so a missing key means it is stuck or gone. arq's own check
connects with `redis_settings`, which has no way to sign in with Entra
(`APP_REDIS_AUTH=entra`); this one connects like the worker does.
"""

from __future__ import annotations

import asyncio
import sys

from arq.constants import default_queue_name, health_check_key_suffix

from app.core.entra import aclose_token_sources
from app.core.redis import arq_redis

#: Give up on an unreachable Redis well inside the probe's own timeout.
CHECK_TIMEOUT_SECONDS = 10


async def check() -> int:
    """0 when the worker's health key is present, 1 otherwise."""
    redis = arq_redis(
        socket_connect_timeout=CHECK_TIMEOUT_SECONDS, socket_timeout=CHECK_TIMEOUT_SECONDS
    )
    try:
        data = await redis.get(default_queue_name + health_check_key_suffix)
    finally:
        await redis.aclose()
        await aclose_token_sources()
    if not data:
        print("worker health check failed: no health key in Redis", file=sys.stderr)
        return 1
    print(f"worker health check passed: {data.decode(errors='replace')}")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(asyncio.run(check()))
