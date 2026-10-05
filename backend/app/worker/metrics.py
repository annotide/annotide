"""Backlog gauges (OPS-4): once a minute, count the queue and the outbox.

Runs on every worker replica (not `unique`): each one reports its own fresh
reading, so the gauges keep flowing while any worker is alive, and their
disappearing is itself the "no worker" signal the alert rules use. Nothing is
queried when OpenTelemetry is off.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import observability
from app.core.config import get_settings
from app.services.ops_metrics import backlog


async def record_backlog(ctx: dict[str, Any]) -> dict[str, Any]:
    """One tick. Registered as an arq cron job in `app.worker.main`."""
    if not observability.is_enabled():
        return {"skipped": "otel off"}
    settings = ctx.get("settings") or get_settings()
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    async with sessionmaker() as session:
        values = await backlog(session, outbox_max_attempts=int(settings.outbox_max_attempts))
    observability.record_backlog(values)
    return values
