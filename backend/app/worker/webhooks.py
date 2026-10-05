"""Webhook delivery tick (API-4), registered as an arq cron in `app.worker.main`."""

from __future__ import annotations

import math
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.services.webhooks import deliver_due, public_destination, public_only_transport

log = structlog.get_logger(__name__)


async def deliver_webhooks(ctx: dict[str, Any]) -> dict[str, Any]:
    """Send every due delivery once; failures are rescheduled by the service."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    settings = ctx.get("settings") or get_settings()
    guarded = not settings.webhook_allow_private_urls
    async with (
        # Guarded: the pre-send check gives a readable error, and the
        # transport re-checks the address it actually dials (DNS rebinding).
        httpx.AsyncClient(
            timeout=float(settings.webhook_timeout),
            follow_redirects=False,
            transport=public_only_transport() if guarded else None,
            trust_env=not guarded,
        ) as client,
        sessionmaker() as session,
    ):
        tally = await deliver_due(
            session,
            client,
            batch_size=int(settings.webhook_poll_batch_size),
            max_attempts=int(settings.webhook_max_attempts),
            frontend_url=settings.frontend_url,
            guard=public_destination if guarded else None,
            per_hook_per_minute=int(settings.webhook_max_per_minute),
            # Four ticks a minute (`main.py`): a quarter of the budget each.
            per_hook_per_batch=max(1, math.ceil(settings.webhook_max_per_minute / 4)),
        )
    if any(tally.values()):
        log.info("webhook.tick", **tally)
    return tally
