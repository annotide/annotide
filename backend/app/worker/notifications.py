"""Notification e-mail tick (API-7), registered as an arq cron in `app.worker.main`."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.services.notification_email import send_due

log = structlog.get_logger(__name__)


async def send_notification_emails(ctx: dict[str, Any]) -> dict[str, int]:
    """Mail the due notifications once; a failure is retried next minute."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    settings = ctx.get("settings") or get_settings()
    async with sessionmaker() as session:
        tally = await send_due(session, settings)
    if any(tally.values()):
        log.info("notification.email_tick", **tally)
    return tally
