"""Liveness and readiness endpoints (OPS-2).

``/health`` answers "is this process running" and must never touch a dependency
— an orchestrator uses it to decide whether to restart the container, and a slow
database would otherwise cause a restart loop.

``/ready`` answers "can this process serve traffic" and does check the database
and the queue, because a replica that cannot reach them should be pulled out of
the load balancer.
"""

from __future__ import annotations

from typing import Annotated, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import SessionDep, SettingsDep
from app.core.redis import redis_client

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"]


class DependencyStatus(BaseModel):
    ok: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, DependencyStatus]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Always 200 while the process is alive. No dependency is consulted."""
    return HealthResponse(status="ok")


async def _check_database(session: SessionDep) -> DependencyStatus:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        return DependencyStatus(ok=False, detail=type(exc).__name__)
    return DependencyStatus(ok=True)


async def _check_redis(settings: SettingsDep) -> DependencyStatus:
    client: aioredis.Redis | None = None
    try:
        client = redis_client(
            settings.redis_url,
            socket_connect_timeout=CHECK_TIMEOUT_SECONDS,
            socket_timeout=CHECK_TIMEOUT_SECONDS,
        )
        await client.ping()
    except Exception as exc:
        return DependencyStatus(ok=False, detail=type(exc).__name__)
    finally:
        if client is not None:
            await client.aclose()
    return DependencyStatus(ok=True)


@router.get("/ready", response_model=ReadyResponse, summary="Readiness probe")
async def ready(
    response: Response,
    database: Annotated[DependencyStatus, Depends(_check_database)],
    queue: Annotated[DependencyStatus, Depends(_check_redis)],
) -> ReadyResponse:
    """Report each dependency, and return 503 when any of them is down."""
    checks = {"database": database, "queue": queue}
    healthy = all(check.ok for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ready" if healthy else "degraded", checks=checks)
