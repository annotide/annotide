"""Task lock reaper (WF-3): expired `in_progress` locks back to the open queue.

An annotator who closes the tab never releases their task; the lock simply
runs out. Nothing in the request path notices that — `claim_next_task` only
offers `open` tasks to other users — so this cron job, once a minute, calls
`services.tasks.reap_expired_locks` and reopens whatever has timed out. Safe
to run on several worker replicas: the bulk `UPDATE` is idempotent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.services.tasks import reap_expired_locks

log = get_logger(__name__)


async def reap_expired_task_locks(ctx: dict[str, Any]) -> dict[str, Any]:
    """One reaper tick. Registered as an arq cron job in `app.worker.main`."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    async with sessionmaker() as session:
        reopened = await reap_expired_locks(session)
    if reopened:
        log.info("task_locks.reaped", reopened=reopened)
    return {"reopened": reopened}
