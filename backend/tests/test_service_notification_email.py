"""`services/notification_email.py` (API-7): who is mailed, what, and retries."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models import Notification, NotificationType, Organization, Project, User
from app.services.notification_email import MAX_ATTEMPTS, send_due


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Project.__table__),
    cast(Table, Notification.__table__),
]
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://t:t@localhost/t",
        "secret_key": "test-key",
        "smtp_host": "smtp.example",
        "smtp_from": "Annotations <noreply@acme.example>",
        "frontend_url": "https://annotate.acme.example/",
    }
    base.update(overrides)
    return Settings(**base)


class FakeMailer:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.sent: list[EmailMessage] = []

    async def send(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        if self.error is not None:
            return [self.error] * len(messages)
        self.sent.extend(messages)
        return [None] * len(messages)


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


class World:
    def __init__(self, project: Project, actor: User, anna: User) -> None:
        self.project = project
        self.actor = actor
        self.anna = anna


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug="acme")
        session.add(org)
        await session.flush()
        actor = User(organization_id=org.id, email="bob@acme.example", display_name="Bob")
        anna = User(organization_id=org.id, email="anna@acme.example", display_name="Anna")
        project = Project(organization_id=org.id, name="Street scenes", settings={}, workflow={})
        session.add_all([actor, anna, project])
        await session.commit()
        return World(project, actor, anna)


async def _notify(
    sessionmaker: async_sessionmaker[AsyncSession],
    world: World,
    *,
    user: User | None = None,
    kind: NotificationType = NotificationType.MENTION,
    created_at: datetime = NOW - timedelta(minutes=1),
    **extra: Any,
) -> UUID:
    payload: dict[str, Any] = {
        "project_id": str(world.project.id),
        "item_id": "11111111-1111-1111-1111-111111111111",
        "actor_id": str(world.actor.id),
        **extra,
    }
    async with sessionmaker() as session:
        row = Notification(
            user_id=(user or world.anna).id, type=kind, payload=payload, created_at=created_at
        )
        session.add(row)
        await session.commit()
        return row.id


async def _row(sessionmaker: async_sessionmaker[AsyncSession], row_id: UUID) -> Notification:
    async with sessionmaker() as session:
        row = await session.get(Notification, row_id)
        assert row is not None
        return row


async def test_a_mention_is_mailed_once_with_a_link(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    row_id = await _notify(sessionmaker, world, excerpt="@anna can you check the bus?")
    mailer = FakeMailer()

    async with sessionmaker() as session:
        assert await send_due(session, make_settings(), mailer, now=NOW) == {
            "sent": 1,
            "failed": 0,
        }
    async with sessionmaker() as session:
        assert await send_due(session, make_settings(), mailer, now=NOW) == {
            "sent": 0,
            "failed": 0,
        }

    [message] = mailer.sent
    assert message["To"] == "anna@acme.example"
    assert message["From"] == "Annotations <noreply@acme.example>"
    assert message["Subject"] == "Bob mentioned you in Street scenes"
    assert message["X-Annotation-Notification"] == str(row_id)
    text = message.get_content()
    assert "> @anna can you check the bus?" in text
    assert (
        f"https://annotate.acme.example/projects/{world.project.id}/annotate/"
        "11111111-1111-1111-1111-111111111111"
    ) in text
    assert (await _row(sessionmaker, row_id)).emailed_at is not None


@pytest.mark.parametrize(
    ("kind", "extra", "subject"),
    [
        (
            NotificationType.REPLY,
            {"excerpt": "done"},
            "Bob replied to your comment in Street scenes",
        ),
        (
            NotificationType.REVIEW,
            {"approve": True, "comment": None},
            "Your annotation was approved in Street scenes",
        ),
        (
            NotificationType.REVIEW,
            {"approve": False, "comment": "Missed a car"},
            "Your annotation was returned for changes in Street scenes",
        ),
    ],
)
async def test_subjects_by_kind(
    sessionmaker: async_sessionmaker[AsyncSession],
    world: World,
    kind: NotificationType,
    extra: dict[str, Any],
    subject: str,
) -> None:
    await _notify(sessionmaker, world, kind=kind, **extra)
    mailer = FakeMailer()
    async with sessionmaker() as session:
        await send_due(session, make_settings(), mailer, now=NOW)
    assert [m["Subject"] for m in mailer.sent] == [subject]


async def test_nothing_without_an_smtp_host(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    await _notify(sessionmaker, world)
    mailer = FakeMailer()
    async with sessionmaker() as session:
        await send_due(session, make_settings(smtp_host=None), mailer, now=NOW)
    assert mailer.sent == []


async def test_skips_opted_out_inactive_service_erased_and_old(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    async with sessionmaker() as session:
        org_id = world.anna.organization_id
        people = {
            "opted-out": User(
                organization_id=org_id, email="o@a.x", display_name="O", email_notifications=False
            ),
            "inactive": User(
                organization_id=org_id, email="i@a.x", display_name="I", is_active=False
            ),
            "service": User(
                organization_id=org_id, email="s@a.x", display_name="S", is_service=True
            ),
            "erased": User(organization_id=org_id, email="e@a.x", display_name="E", erased_at=NOW),
        }
        session.add_all(people.values())
        await session.commit()
    for person in people.values():
        await _notify(sessionmaker, world, user=person)
    await _notify(sessionmaker, world, created_at=NOW - timedelta(days=2))  # too old
    mailer = FakeMailer()

    async with sessionmaker() as session:
        await send_due(session, make_settings(), mailer, now=NOW)
    assert mailer.sent == []


async def test_a_failed_send_is_retried_then_dropped(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    row_id = await _notify(sessionmaker, world)
    broken = FakeMailer(error="SMTPServerDisconnected: gone")

    for _ in range(MAX_ATTEMPTS + 2):
        async with sessionmaker() as session:
            await send_due(session, make_settings(), broken, now=NOW)

    row = await _row(sessionmaker, row_id)
    assert row.emailed_at is None
    assert row.email_attempts == MAX_ATTEMPTS
    async with sessionmaker() as session:
        healthy = FakeMailer()
        await send_due(session, make_settings(), healthy, now=NOW)
        assert healthy.sent == []
        assert (await session.scalar(select(Notification.email_attempts))) == MAX_ATTEMPTS


async def test_a_line_break_in_a_name_cannot_inject_headers(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    async with sessionmaker() as session:
        actor = await session.get(User, world.actor.id)
        assert actor is not None
        actor.display_name = "Bob\r\nBcc: victim@evil.example"
        await session.commit()
    await _notify(sessionmaker, world)
    mailer = FakeMailer()
    async with sessionmaker() as session:
        assert (await send_due(session, make_settings(), mailer, now=NOW))["sent"] == 1
    [message] = mailer.sent
    assert message["Bcc"] is None
    assert "\n" not in str(message["Subject"])
