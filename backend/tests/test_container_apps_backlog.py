"""The Container Apps wake-up query (`infra/azure-container-apps/worker-backlog.sql`).

KEDA runs it against PostgreSQL to keep the scale-to-zero app awake while the
worker has something to do. It names tables, columns and enum values by hand,
so it runs here against the ORM's schema: a rename breaks this test rather
than silently leaving work unprocessed while the app sleeps.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models import Job, OutboxEvent, WebhookDelivery
from app.models.ops import JobStatus, JobType
from app.models.webhook import WebhookDeliveryStatus

QUERY = (
    Path(__file__).resolve().parents[2] / "infra" / "azure-container-apps" / "worker-backlog.sql"
).read_text()

_TABLES: list[Table] = [
    cast(Table, Job.__table__),
    cast(Table, OutboxEvent.__table__),
    cast(Table, WebhookDelivery.__table__),
]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _backlog(session: AsyncSession) -> int:
    return int((await session.execute(text(QUERY))).scalar_one())


def _delivery(status: WebhookDeliveryStatus, next_attempt_at: datetime) -> WebhookDelivery:
    return WebhookDelivery(
        webhook_id=uuid4(),
        event="snapshot.created",
        payload={},
        status=status,
        next_attempt_at=next_attempt_at,
    )


async def test_counts_only_work_the_worker_would_pick_up(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    max_attempts = Settings.model_fields["outbox_max_attempts"].default
    async with sessionmaker() as session:
        assert await _backlog(session) == 0
        session.add_all(
            [
                # Counted: two live jobs, an unpublished event, a due delivery.
                Job(type=JobType.EXPORT, status=JobStatus.QUEUED),
                Job(type=JobType.EXPORT, status=JobStatus.RUNNING),
                OutboxEvent(
                    aggregate_type="annotation",
                    aggregate_id=uuid4(),
                    type="annotation.saved",
                    payload={},
                    attempts=max_attempts - 1,
                ),
                _delivery(WebhookDeliveryStatus.PENDING, now - timedelta(minutes=1)),
                # Not counted: finished jobs, published or given-up events,
                # deliveries in backoff or done.
                Job(type=JobType.EXPORT, status=JobStatus.SUCCEEDED),
                Job(type=JobType.EXPORT, status=JobStatus.FAILED),
                OutboxEvent(
                    aggregate_type="annotation",
                    aggregate_id=uuid4(),
                    type="annotation.saved",
                    payload={},
                    published_at=now,
                ),
                OutboxEvent(
                    aggregate_type="annotation",
                    aggregate_id=uuid4(),
                    type="annotation.saved",
                    payload={},
                    attempts=max_attempts,
                ),
                _delivery(WebhookDeliveryStatus.PENDING, now + timedelta(hours=1)),
                _delivery(WebhookDeliveryStatus.SUCCEEDED, now - timedelta(minutes=1)),
            ]
        )
        await session.commit()
        assert await _backlog(session) == 4


def test_outbox_attempt_limit_matches_the_worker() -> None:
    limit = Settings.model_fields["outbox_max_attempts"].default
    assert f"attempts < {limit}" in QUERY
