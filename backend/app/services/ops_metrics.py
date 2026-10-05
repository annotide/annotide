"""Queue and outbox backlog, counted from the database (OPS-4).

The `job` and `outbox_event` rows are the source of truth, so the backlog is
read from them rather than from Redis: a job Redis lost still counts as
waiting. The numbers feed `observability.record_backlog`, whose gauges the
Helm chart's alert rules watch.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus, OutboxEvent


def _age_seconds(since: datetime | None, now: datetime) -> float:
    if since is None:
        return 0.0
    if since.tzinfo is None:  # SQLite returns naive timestamps
        since = since.replace(tzinfo=UTC)
    return max(0.0, (now - since).total_seconds())


async def backlog(
    session: AsyncSession, *, outbox_max_attempts: int, now: datetime | None = None
) -> dict[str, float]:
    """The values for `observability.BACKLOG_GAUGES`, keyed by gauge name.

    A queued job's wait counts from `updated_at`, which a retry or a requeue
    after a worker shutdown moves, so a retried job is not "waiting" since the
    day it was first created.
    """
    now = now or datetime.now(UTC)
    jobs = (
        await session.execute(
            select(Job.status, func.count(), func.min(Job.updated_at))
            .where(Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
            .group_by(Job.status)
        )
    ).all()
    by_status = {status: (count, oldest) for status, count, oldest in jobs}
    queued, oldest_queued = by_status.get(JobStatus.QUEUED, (0, None))
    running, _ = by_status.get(JobStatus.RUNNING, (0, None))

    unpublished = OutboxEvent.published_at.is_(None)
    live = OutboxEvent.attempts < outbox_max_attempts
    pending, oldest_pending = (
        await session.execute(
            select(func.count(), func.min(OutboxEvent.created_at)).where(unpublished, live)
        )
    ).one()
    dead = await session.scalar(select(func.count()).where(unpublished, ~live)) or 0

    return {
        "annotation.jobs.queued": float(queued),
        "annotation.jobs.running": float(running),
        "annotation.jobs.queued.oldest_age": _age_seconds(oldest_queued, now),
        "annotation.outbox.pending": float(pending),
        "annotation.outbox.pending.oldest_age": _age_seconds(oldest_pending, now),
        "annotation.outbox.dead": float(dead),
    }
