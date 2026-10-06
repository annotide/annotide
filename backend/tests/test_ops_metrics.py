"""Backlog counts for the OPS-4 gauges (`services/ops_metrics.py`, `worker/metrics.py`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import observability
from app.db.base import Base
from app.models import Job, JobStatus, JobType, OutboxEvent
from app.services.ops_metrics import backlog
from app.worker.metrics import record_backlog

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    tables = [cast(Table, Job.__table__), cast(Table, OutboxEvent.__table__)]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    observability.shutdown()
    yield
    observability.shutdown()


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        for status, minutes in (
            (JobStatus.QUEUED, 12),
            (JobStatus.QUEUED, 3),
            (JobStatus.RUNNING, 20),
            (JobStatus.SUCCEEDED, 90),
            (JobStatus.FAILED, 90),
        ):
            moment = NOW - timedelta(minutes=minutes)
            session.add(
                Job(
                    type=JobType.EXPORT,
                    status=status,
                    payload={},
                    created_at=moment - timedelta(days=1),
                    updated_at=moment,
                )
            )
        for minutes, attempts, published in (
            (7, 2, False),
            (1, 0, False),
            (5, 10, False),
            (9, 1, True),
        ):
            session.add(
                OutboxEvent(
                    aggregate_type="annotation",
                    aggregate_id=uuid.uuid4(),
                    type="annotation.written",
                    payload={},
                    attempts=attempts,
                    published_at=NOW if published else None,
                    created_at=NOW - timedelta(minutes=minutes),
                )
            )
        await session.commit()


async def test_backlog_counts_waiting_work_from_the_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        values = await backlog(session, outbox_max_attempts=10, now=NOW)

    assert values == {
        "annotation.jobs.queued": 2,
        "annotation.jobs.running": 1,
        # From updated_at (a retry resets the wait), not created_at a day earlier.
        "annotation.jobs.queued.oldest_age": 12 * 60,
        "annotation.outbox.pending": 2,
        "annotation.outbox.pending.oldest_age": 7 * 60,
        "annotation.outbox.dead": 1,
    }
    assert set(values) == set(observability.BACKLOG_GAUGES)


async def test_an_empty_backlog_is_all_zeros(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        values = await backlog(session, outbox_max_attempts=10, now=NOW)
    assert set(values.values()) == {0.0}


def _gauges(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    found: dict[str, float] = {}
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    if metric.name in observability.BACKLOG_GAUGES:
                        found[metric.name] = cast(Any, point).value
    return found


async def test_the_cron_skips_the_database_when_tracing_is_off() -> None:
    assert await record_backlog({}) == {"skipped": "otel off"}


async def test_the_cron_feeds_fresh_readings_to_the_gauges(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = InMemoryMetricReader()
    observability.configure(
        "annotation-test",
        span_exporter=InMemorySpanExporter(),
        metric_reader=reader,
        set_global=False,
    )
    assert _gauges(reader) == {}, "nothing reported before the first reading"

    await _seed(sessionmaker)
    values = await record_backlog({"sessionmaker": sessionmaker})
    assert values["annotation.jobs.queued"] == 2
    assert _gauges(reader)["annotation.jobs.queued"] == 2
    assert _gauges(reader)["annotation.outbox.dead"] == 1

    # A reading older than the freshness window is withheld, not repeated.
    monkeypatch.setattr(observability, "BACKLOG_FRESH_SECONDS", -1.0)
    assert _gauges(reader) == {}
