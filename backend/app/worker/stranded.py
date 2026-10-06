"""Stranded job recovery: rows the queue lost after a Redis flush or restore.

Every five minutes, `services.jobs.recover_stranded_jobs` re-enqueues
`queued` rows with no arq entry and fails `running` rows that outlived the
job timeout without one (docs/CONTRACTS.md → "### job"). Uses the worker's
own arq pool; safe on several replicas.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.services.jobs import recover_stranded_jobs
from app.services.queue import ArqJobQueue

log = get_logger(__name__)


async def requeue_stranded_jobs(ctx: dict[str, Any]) -> dict[str, Any]:
    """One recovery tick. Registered as an arq cron job in `app.worker.main`."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    queue = ArqJobQueue(ctx["redis"])
    timeout = timedelta(seconds=int(ctx["job_timeout_seconds"]))
    async with sessionmaker() as session:
        outcome = await recover_stranded_jobs(session, queue, job_timeout=timeout)
    for job_id in outcome.requeued:
        log.warning("job.requeued_stranded", job_id=str(job_id))
    for job_id in outcome.failed:
        log.warning("job.failed_stranded", job_id=str(job_id))
    return {"requeued": len(outcome.requeued), "failed": len(outcome.failed)}
