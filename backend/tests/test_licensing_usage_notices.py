"""Signs of seat sharing (LIC-31): `services/licensing/usage_notices.py` and
`GET /license/usage-notices`, on in-memory SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    MediaType,
    Organization,
    Project,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.services.licensing.usage_notices import (
    NoticeKind,
    network,
    parallel_sign_in_days,
    peak_per_hour,
    usage_notices,
)

#: Real time, so the endpoint (which reads the clock) sees the fixture's events.
NOW = datetime.now(UTC).replace(microsecond=0)
HOUR = timedelta(hours=1)


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, t)
    for t in (
        Organization.__table__,
        User.__table__,
        AuditEvent.__table__,
        Connector.__table__,
        Project.__table__,
        Item.__table__,
        Task.__table__,
    )
]


# --- Pure pieces -------------------------------------------------------------------


def test_network_coarsens_and_keeps_private_ranges() -> None:
    assert network("203.0.113.7") == "203.0.113.0/24"
    assert network("10.1.2.3") == "10.1.2.0/24"
    assert network("2001:db8:1:2::5") == "2001:db8:1::/48"
    assert network("192.0.2.1/32") == "192.0.2.0/24"
    assert network("nonsense") is None
    assert network(None) is None


def test_parallel_sign_ins_need_two_networks_inside_one_session() -> None:
    day = NOW.replace(hour=9)
    sign_ins = [
        (day, "203.0.113.7"),
        (day + timedelta(minutes=20), "198.51.100.4"),  # other network, same session
        (day + timedelta(days=1), "203.0.113.7"),
        (day + timedelta(days=1, hours=2), "198.51.100.4"),  # other network, later session
        (day + timedelta(days=2), "203.0.113.7"),
        (day + timedelta(days=2, minutes=5), "203.0.113.99"),  # same /24
        (day + timedelta(days=3), None),
    ]
    assert parallel_sign_in_days(sign_ins, HOUR) == 1


def test_peak_per_hour_slides() -> None:
    start = NOW
    times = [start + timedelta(seconds=10 * i) for i in range(400)]  # 66 minutes of work
    assert peak_per_hour(times) == 360
    assert peak_per_hour([]) == 0


# --- Database ----------------------------------------------------------------------


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


class World:
    def __init__(self, org: Organization, users: dict[str, User]) -> None:
        self.org = org
        self.users = users


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug="acme")
        session.add(org)
        await session.flush()
        users = {
            name: User(
                organization_id=org.id,
                email=f"{name}@acme.com",
                display_name=name.title(),
                is_service=name == "bot",
            )
            for name in ("anna", "ben", "cleo", "bot", "dan")
        }
        session.add_all(users.values())
        await session.flush()

        def event(user: str, action: str, at: datetime, ip: str | None = None) -> None:
            session.add(
                AuditEvent(
                    organization_id=org.id,
                    actor_id=users[user].id,
                    action=action,
                    target_type="user",
                    created_at=at,
                    ip=ip,
                )
            )

        # Anna: two networks within one session on three days.
        for days_ago in (1, 3, 5):
            at = NOW - timedelta(days=days_ago)
            event("anna", "auth.login", at, "203.0.113.7")
            event("anna", "auth.login", at + timedelta(minutes=10), "198.51.100.4")
        # Ben: 1300 submits inside an hour.
        for i in range(1300):
            event("ben", "annotation.submit", NOW - timedelta(days=2) + timedelta(seconds=2 * i))
        # Bot: a service account submitting on six days.
        for days_ago in range(1, 7):
            event("bot", "annotation.submit", NOW - timedelta(days=days_ago))
        # Dan: steady human pace, and old sharing outside the 30-day window.
        for i in range(50):
            event("dan", "annotation.submit", NOW - timedelta(days=1) + timedelta(minutes=i))
        for days_ago in (40, 41, 42):
            at = NOW - timedelta(days=days_ago)
            event("dan", "auth.login", at, "203.0.113.7")
            event("dan", "auth.login", at + timedelta(minutes=10), "198.51.100.4")

        # Cleo holds three live task locks; Dan one live and two expired.
        connector = Connector(
            organization_id=org.id,
            name="source",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp/fixture"},
        )
        session.add(connector)
        project = Project(organization_id=org.id, name="P")
        session.add(project)
        await session.flush()
        locks = [("cleo", HOUR)] * 3 + [("dan", HOUR), ("dan", -HOUR), ("dan", -HOUR)]
        for index, (holder, remaining) in enumerate(locks):
            item = Item(
                project_id=project.id,
                connector_id=connector.id,
                path=f"images/{index}.jpg",
                media_type=MediaType.IMAGE,
                size_bytes=1,
                meta={},
            )
            session.add(item)
            await session.flush()
            session.add(
                Task(
                    project_id=project.id,
                    item_id=item.id,
                    type=TaskType.ANNOTATE,
                    status=TaskStatus.IN_PROGRESS,
                    locked_by_id=users[holder].id,
                    locked_until=NOW + remaining,
                )
            )
        await session.commit()
        return World(org, users)


async def test_each_kind_of_notice_is_found_once(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    async with sessionmaker() as session:
        notices = await usage_notices(session, session_length=HOUR, now=NOW)

    assert [(n.kind, n.email, n.count) for n in notices] == [
        (NoticeKind.PARALLEL_SIGN_INS, "anna@acme.com", 3),
        (NoticeKind.PARALLEL_TASKS, "cleo@acme.com", 3),
        (NoticeKind.SUPERHUMAN_PACE, "ben@acme.com", 1300),
        (NoticeKind.SERVICE_ACCOUNT_ANNOTATING, "bot@acme.com", 6),
    ]
    assert "less than 60 minutes apart on 3 days" in notices[0].detail


async def test_a_service_account_is_not_flagged_for_speed(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    async with sessionmaker() as session:
        bot = world.users["bot"]
        for i in range(1300):
            session.add(
                AuditEvent(
                    organization_id=world.org.id,
                    actor_id=bot.id,
                    action="annotation.submit",
                    target_type="annotation",
                    created_at=NOW - timedelta(hours=3) + timedelta(seconds=i),
                )
            )
        await session.commit()
        notices = await usage_notices(session, session_length=HOUR, now=NOW)

    assert {(n.kind, n.email) for n in notices if n.email == "bot@acme.com"} == {
        (NoticeKind.SERVICE_ACCOUNT_ANNOTATING, "bot@acme.com")
    }


# --- API --------------------------------------------------------------------------


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession], world: World) -> Iterator[FastAPI]:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    def signed_in(is_superuser: bool) -> CurrentUser:
        return CurrentUser(
            id=uuid.uuid4(),
            organization_id=world.org.id,
            email="admin@acme.com",
            is_superuser=is_superuser,
            is_service=False,
            scopes=frozenset(),
        )

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_current_user] = lambda: signed_in(True)
    application.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://t:t@localhost/t",
        secret_key="test-key",
        access_token_ttl=1800,
    )
    yield application
    application.dependency_overrides.clear()


def test_endpoint_lists_notices(app: FastAPI, world: World) -> None:
    response = TestClient(app).get("/api/v1/license/usage-notices")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window_days"] == 30
    assert [n["kind"] for n in body["notices"]] == [
        "parallel_sign_ins",
        "parallel_tasks",
        "superhuman_pace",
        "service_account_annotating",
    ]
    first = body["notices"][0]
    assert first["user_id"] == str(world.users["anna"].id)
    assert first["display_name"] == "Anna"
    # `APP_ACCESS_TOKEN_TTL` is the session length.
    assert "less than 30 minutes apart" in first["detail"]


def test_endpoint_requires_a_superuser(app: FastAPI, world: World) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid.uuid4(),
        organization_id=world.org.id,
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    assert TestClient(app).get("/api/v1/license/usage-notices").status_code == 403
