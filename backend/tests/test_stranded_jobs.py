"""Tests for stranded job recovery (`services/jobs.py`, `worker/stranded.py`, ARC-4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import Job, JobStatus, JobType
from app.services.jobs import LOST_JOB_ERROR, recover_stranded_jobs
from app.services.queue import JobAlreadyQueuedError
from app.worker.stranded import requeue_stranded_jobs
from tests.support import FakeJobQueue

TIMEOUT = timedelta(minutes=30)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[cast(Table, Job.__table__)])
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _job(
    sessionmaker: async_sessionmaker[AsyncSession],
    status: JobStatus,
    *,
    age: timedelta,
) -> UUID:
    moment = NOW - age
    async with sessionmaker() as session:
        job = Job(
            type=JobType.EXPORT,
            status=status,
            payload={},
            started_at=moment if status == JobStatus.RUNNING else None,
            created_at=moment,
            updated_at=moment,
        )
        session.add(job)
        await session.commit()
        return job.id


async def _status(sessionmaker: async_sessionmaker[AsyncSession], job_id: UUID) -> Job:
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job


async def _recover(
    sessionmaker: async_sessionmaker[AsyncSession], queue: FakeJobQueue
) -> tuple[list[UUID], list[UUID]]:
    async with sessionmaker() as session:
        outcome = await recover_stranded_jobs(session, queue, job_timeout=TIMEOUT, now=NOW)
    return outcome.requeued, outcome.failed


async def test_old_queued_job_missing_from_redis_is_requeued(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    lost = await _job(sessionmaker, JobStatus.QUEUED, age=timedelta(minutes=10))
    waiting = await _job(sessionmaker, JobStatus.QUEUED, age=timedelta(minutes=10))
    fresh = await _job(sessionmaker, JobStatus.QUEUED, age=timedelta(minutes=1))
    queue = FakeJobQueue()
    queue.known.add(waiting)

    requeued, failed = await _recover(sessionmaker, queue)

    assert requeued == [lost]
    assert failed == []
    assert [job_id for job_id, _ in queue.enqueued] == [lost]
    assert fresh not in requeued


async def test_long_running_job_missing_from_redis_fails(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    lost = await _job(sessionmaker, JobStatus.RUNNING, age=timedelta(minutes=40))
    alive = await _job(sessionmaker, JobStatus.RUNNING, age=timedelta(minutes=40))
    within_timeout = await _job(sessionmaker, JobStatus.RUNNING, age=timedelta(minutes=20))
    done = await _job(sessionmaker, JobStatus.SUCCEEDED, age=timedelta(hours=5))
    queue = FakeJobQueue()
    queue.known.add(alive)

    requeued, failed = await _recover(sessionmaker, queue)

    assert requeued == []
    assert failed == [lost]
    job = await _status(sessionmaker, lost)
    assert job.status == JobStatus.FAILED
    assert job.error == LOST_JOB_ERROR
    assert job.finished_at is not None
    for untouched in (alive, within_timeout):
        assert (await _status(sessionmaker, untouched)).status == JobStatus.RUNNING
    assert (await _status(sessionmaker, done)).status == JobStatus.SUCCEEDED
    assert queue.enqueued == []


async def test_a_duplicate_from_another_worker_is_not_counted(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _job(sessionmaker, JobStatus.QUEUED, age=timedelta(minutes=10))
    queue = FakeJobQueue()

    async def raced(job: Job) -> None:
        raise JobAlreadyQueuedError(str(job.id))

    queue.enqueue = raced  # type: ignore[method-assign]  # simulate another worker winning

    requeued, failed = await _recover(sessionmaker, queue)

    assert (requeued, failed) == ([], [])


class _FakePool:
    """The two arq pool calls `ArqJobQueue` makes here."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def exists(self, key: str) -> int:
        return 0

    async def enqueue_job(self, function: str, *args: Any, _job_id: str) -> object:
        self.enqueued.append(_job_id)
        return object()


async def test_cron_wires_the_arq_pool(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    lost = await _job(sessionmaker, JobStatus.QUEUED, age=timedelta(days=1))
    pool = _FakePool()
    ctx = {"sessionmaker": sessionmaker, "redis": pool, "job_timeout_seconds": 1800}

    assert await requeue_stranded_jobs(ctx) == {"requeued": 1, "failed": 0}
    assert pool.enqueued == [str(lost)]
