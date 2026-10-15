"""Licence refresh (LIC-27) and heartbeat (LIC-6), every five minutes.

Each decides for itself whether it is due — the refresh daily, the heartbeat
weekly, a failure retried after minutes when the licence server was starting
up and hourly otherwise (`vendor.next_attempt`) — so the cron can tick often
and a worker that was down simply catches up. Nothing here affects how the
install behaves, apart from storing a renewed key.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.licensing import heartbeat, refresh, vendor
from app.services.licensing.state import get_state

log = get_logger(__name__)


async def licence_calls(ctx: dict[str, Any]) -> dict[str, Any]:
    """One tick. Registered as an arq cron job in `app.worker.main`."""
    settings: Settings = ctx["settings"]
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    if not settings.license_server_url:
        return {"refreshed": False, "heartbeat": False}
    async with vendor.open_client(ctx.get("licence_transport"), background=True) as client:
        async with sessionmaker() as session:
            refreshed = await refresh.refresh_if_due(session, settings, client)
            await session.commit()
        async with sessionmaker() as session:
            sent = await heartbeat.send_if_due(session, settings, client)
            await session.commit()
            state = await get_state(session) if refreshed or sent else None
    if state is not None:
        # Why a call failed and when it runs again, so a log shows it too.
        log.info(
            "licence.calls",
            refreshed=refreshed,
            heartbeat=sent,
            refresh_error=state.refresh_error if refreshed else None,
            refresh_retry_at=state.refresh_retry_at if refreshed else None,
            heartbeat_error=state.heartbeat_error if sent else None,
            heartbeat_retry_at=state.heartbeat_retry_at if sent else None,
        )
    return {"refreshed": refreshed, "heartbeat": sent}
