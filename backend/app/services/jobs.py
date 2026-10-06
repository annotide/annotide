"""Job lifecycle from the API's side: submit, cancel, retry (ARC-4).

The `job` row is created and committed *before* the queue is told about it,
so a worker that dequeues quickly always finds the row. If the queue cannot
be reached afterwards, the row is marked failed rather than left `queued`
forever with nothing coming for it — a queued job that never runs is the
most confusing state a user can be shown.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, ServiceUnavailableError
from app.models import Job, JobStatus, JobType
from app.services import audit, idempotency
from app.services.queue import JobAlreadyQueuedError, JobQueue, QueueUnavailableError

#: States a job can be retried from. `succeeded` is deliberately absent: re-running
#: a finished export is a new job, not a retry.
RETRYABLE = frozenset({JobStatus.FAILED, JobStatus.CANCELLED})

#: States a job can be cancelled from.
CANCELLABLE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


async def _enqueue_or_fail(
    session: AsyncSession, queue: JobQueue, job: Job, *, requeue: bool = False
) -> None:
    try:
        if requeue:
            await queue.requeue(job)
        else:
            await queue.enqueue(job)
    except QueueUnavailableError as exc:
        job.status = JobStatus.FAILED
        job.error = f"Could not enqueue: {exc}"
        await session.commit()
        raise ServiceUnavailableError(
            "The job queue is not reachable; the job was recorded as failed. Retry it later."
        ) from exc
    except JobAlreadyQueuedError as exc:
        raise ConflictError(f"Job {job.id} is already queued.") from exc


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who queued a job, for the audit row `submit_job` writes."""

    organization_id: UUID
    user_id: UUID
    ip: str | None = None


def _endpoint(job_type: JobType) -> str:
    return f"job.{job_type.value}"


async def find_replayed_job(
    session: AsyncSession, *, organization_id: UUID, job_type: JobType, idempotency_key: str
) -> Job | None:
    """The job an earlier create of this type made with the key (API-2), if any."""
    job_id = await idempotency.find(
        session,
        organization_id=organization_id,
        endpoint=_endpoint(job_type),
        key=idempotency_key,
    )
    return None if job_id is None else await session.get(Job, job_id)


async def submit_job(
    session: AsyncSession,
    queue: JobQueue,
    *,
    project_id: UUID | None,
    job_type: JobType,
    payload: dict[str, Any],
    actor: AuditActor | None = None,
    idempotency_key: str | None = None,
) -> Job:
    """Create a `queued` job row, commit it, then enqueue it.

    With `actor` given, the `job.create` audit row (SEC-3) joins the same
    transaction as the job row, so the two can never disagree. With
    `idempotency_key` (needs `actor` for the organisation), the key row joins
    it too; if a concurrent retry wins the unique index, the winner's job is
    returned and nothing is enqueued twice.
    """
    job = Job(project_id=project_id, type=job_type, status=JobStatus.QUEUED, payload=payload)
    session.add(job)
    await session.flush()
    if idempotency_key is not None and actor is not None:
        idempotency.remember(
            session,
            organization_id=actor.organization_id,
            endpoint=_endpoint(job_type),
            key=idempotency_key,
            target_id=job.id,
        )
    if actor is not None:
        audit.record(
            session,
            organization_id=actor.organization_id,
            actor_id=actor.user_id,
            action="job.create",
            target_type="job",
            target_id=job.id,
            after={"type": job_type.value, "project_id": str(project_id) if project_id else None},
            ip=actor.ip,
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idempotency_key is not None and actor is not None:
            existing = await find_replayed_job(
                session,
                organization_id=actor.organization_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return existing
        raise
    await session.refresh(job)

    await _enqueue_or_fail(session, queue, job)
    return job


async def cancel_job(session: AsyncSession, queue: JobQueue, job: Job) -> Job:
    """Mark a queued or running job cancelled and ask the worker to stop it.

    The row is the record of the cancellation: a worker that picks the job up
    later sees `cancelled` and skips it even if the abort signal was lost.
    """
    if job.status not in CANCELLABLE:
        raise ConflictError(f"A {job.status.value} job cannot be cancelled.")

    job.status = JobStatus.CANCELLED
    await session.commit()

    # The row already says cancelled; the worker honours that on its own, so a
    # lost abort signal only means a running job finishes before it notices.
    with contextlib.suppress(QueueUnavailableError):
        await queue.abort(job.id)

    await session.refresh(job)
    return job


async def retry_job(session: AsyncSession, queue: JobQueue, job: Job) -> Job:
    """Re-queue a failed or cancelled job with the same payload.

    `attempts` is kept so the history is visible; `error`, `result` and
    progress are cleared because they describe the previous run.
    """
    if job.status not in RETRYABLE:
        raise ConflictError(f"A {job.status.value} job cannot be retried.")

    job.status = JobStatus.QUEUED
    job.error = None
    job.result = None
    job.progress = 0
    job.started_at = None
    job.finished_at = None
    await session.commit()
    await session.refresh(job)

    await _enqueue_or_fail(session, queue, job, requeue=True)
    return job


#: How long a `queued` row may sit before its queue entry is checked: longer
#: than the gap between the API committing the row and enqueueing it.
STRANDED_QUEUED_AFTER = timedelta(minutes=5)
#: Added to the worker's job timeout before a `running` row counts as lost.
STRANDED_RUNNING_GRACE = timedelta(minutes=5)
LOST_JOB_ERROR = "lost by the queue (worker or Redis restarted); retry it"


@dataclass(frozen=True, slots=True)
class StrandedJobs:
    requeued: list[UUID]
    failed: list[UUID]


async def recover_stranded_jobs(
    session: AsyncSession, queue: JobQueue, *, job_timeout: timedelta, now: datetime | None = None
) -> StrandedJobs:
    """Re-enqueue `queued` rows and fail `running` rows the queue no longer holds.

    The `job` row is the source of truth; this closes the gap when Redis is
    flushed or restored from an older state (docs/CONTRACTS.md → "### job").
    Safe on several workers: arq refuses a duplicate id, and failing a row is
    guarded by its status.
    """
    moment = now or datetime.now(UTC)
    requeued: list[UUID] = []
    failed: list[UUID] = []

    queued = (
        await session.scalars(
            select(Job).where(
                Job.status == JobStatus.QUEUED,
                Job.updated_at < moment - STRANDED_QUEUED_AFTER,
            )
        )
    ).all()
    for job in queued:
        if await queue.exists(job.id):
            continue
        try:
            await queue.enqueue(job)
        except JobAlreadyQueuedError:
            continue  # another worker got there first
        requeued.append(job.id)

    running = (
        await session.scalars(
            select(Job).where(
                Job.status == JobStatus.RUNNING,
                Job.started_at < moment - job_timeout - STRANDED_RUNNING_GRACE,
            )
        )
    ).all()
    for job in running:
        if await queue.exists(job.id):
            continue
        job.status = JobStatus.FAILED
        job.error = LOST_JOB_ERROR
        job.finished_at = moment
        failed.append(job.id)
    if failed:
        await session.commit()
    return StrandedJobs(requeued=requeued, failed=failed)
