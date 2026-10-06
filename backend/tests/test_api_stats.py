"""Tests for `GET /projects/{id}/stats` (UX-5).

Seeds a small project by hand — items in several states, two annotators, a
model version, a rejected item — and checks every panel of the response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationSource,
    AnnotationStatus,
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    ItemStatus,
    MediaType,
    Membership,
    Organization,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    User,
)


@compiles(CITEXT, "sqlite")  # pragma: no cover - same shim as test_api_members.py
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, AuditEvent.__table__),
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Connector.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, Item.__table__),
    cast(Table, Task.__table__),
    cast(Table, Annotation.__table__),
]

SCHEMA_VERSION_ID = uuid4()
MODEL_VERSION_ID = uuid4()


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _sign_in(app: FastAPI, user_id: UUID, organization_id: UUID) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="person@example.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )


def _result(*classes: str, classification: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "media_type": "image",
        "classification": classification or {},
        "shapes": [
            {"id": str(uuid4()), "type": "bbox", "class": cls, "bbox": [1, 2, 30, 40]}
            for cls in classes
        ],
    }


class _Seed:
    organization_id: UUID
    project_id: UUID
    connector_id: UUID
    alice: UUID
    bob: UUID


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seed:
    """Two annotators, six items, a mix of drafts, submissions, reviews and one model version."""
    seed = _Seed()
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        alice = User(
            organization_id=org.id, email="alice@example.com", display_name="Alice", is_active=True
        )
        bob = User(
            organization_id=org.id, email="bob@example.com", display_name="Bob", is_active=True
        )
        connector = Connector(
            organization_id=org.id,
            name="source",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp/fixture"},
        )
        project = Project(organization_id=org.id, name="Project 1")
        session.add_all([alice, bob, connector, project])
        await session.flush()
        session.add(Membership(user_id=alice.id, project_id=project.id, role=ProjectRole.OWNER))

        def item(path: str, status: ItemStatus) -> Item:
            row = Item(
                project_id=project.id,
                connector_id=connector.id,
                path=path,
                media_type=MediaType.IMAGE,
                size_bytes=1,
                meta={},
                status=status,
            )
            session.add(row)
            return row

        approved_a = item("a.jpg", ItemStatus.APPROVED)
        approved_b = item("b.jpg", ItemStatus.APPROVED)
        rejected = item("c.jpg", ItemStatus.REJECTED)
        submitted = item("d.jpg", ItemStatus.SUBMITTED)
        drafted = item("e.jpg", ItemStatus.ANNOTATING)
        item("f.jpg", ItemStatus.NEW)
        prelabeled = item("g.jpg", ItemStatus.PRELABELED)
        await session.flush()

        now = datetime.now(UTC)

        def version(
            row: Item,
            n: int,
            status: AnnotationStatus,
            result: dict[str, Any],
            *,
            author: UUID | None = None,
            age: timedelta = timedelta(0),
        ) -> None:
            session.add(
                Annotation(
                    item_id=row.id,
                    version=n,
                    author_user_id=author,
                    author_model_version_id=None if author else MODEL_VERSION_ID,
                    source=AnnotationSource.HUMAN if author else AnnotationSource.MODEL,
                    label_schema_version_id=SCHEMA_VERSION_ID,
                    result=result,
                    status=status,
                    created_at=now - age,
                )
            )

        # a: v1 rejected (old), v2 approved — only v2 counts as "latest".
        version(
            approved_a,
            1,
            AnnotationStatus.REJECTED,
            _result("car"),
            author=alice.id,
            age=timedelta(days=3),
        )
        version(
            approved_a,
            2,
            AnnotationStatus.APPROVED,
            _result("car", "car"),
            author=alice.id,
            age=timedelta(days=1),
        )
        version(
            approved_b,
            1,
            AnnotationStatus.APPROVED,
            _result("bus", classification={"weather": "rain"}),
            author=bob.id,
        )
        version(rejected, 1, AnnotationStatus.REJECTED, _result("car"), author=bob.id)
        version(submitted, 1, AnnotationStatus.SUBMITTED, _result("bus"), author=alice.id)
        version(drafted, 1, AnnotationStatus.DRAFT, _result("car", "bus", "bus"), author=alice.id)
        version(
            prelabeled, 1, AnnotationStatus.DRAFT, _result("car"), age=timedelta(days=40)
        )  # model output, outside the throughput window

        session.add_all(
            [
                Task(project_id=project.id, item_id=r.id, type=t, status=s)
                for r, t, s in [
                    (approved_a, TaskType.ANNOTATE, TaskStatus.DONE),
                    (approved_a, TaskType.REVIEW, TaskStatus.DONE),
                    (submitted, TaskType.REVIEW, TaskStatus.OPEN),
                    (drafted, TaskType.ANNOTATE, TaskStatus.IN_PROGRESS),
                    (rejected, TaskType.ANNOTATE, TaskStatus.OPEN),
                    (rejected, TaskType.REVIEW, TaskStatus.CANCELLED),
                ]
            ]
        )
        await session.commit()
        seed.organization_id = org.id
        seed.project_id = project.id
        seed.connector_id = connector.id
        seed.alice = alice.id
        seed.bob = bob.id
    return seed


async def test_stats_cover_every_panel(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(sessionmaker)
    _sign_in(app, seed.alice, seed.organization_id)

    response = client.get(f"/api/v1/projects/{seed.project_id}/stats")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["items"]["total"] == 7
    assert body["items"]["by_status"] == {
        "new": 1,
        "prelabeled": 1,
        "annotating": 1,
        "submitted": 1,
        "in_review": 0,
        "approved": 2,
        "rejected": 1,
        "skipped": 0,
    }
    assert body["tasks"] == {
        "annotate": {"open": 1, "in_progress": 1, "done": 1, "cancelled": 0},
        "review": {"open": 1, "in_progress": 0, "done": 1, "cancelled": 1},
    }
    assert body["annotations"]["versions"] == 7
    assert body["annotations"]["by_source"] == {"human": 6, "model": 1}
    # Latest per item: a→approved, b→approved, c→rejected, d→submitted, e→draft, g→draft.
    assert body["annotations"]["latest_by_status"] == {
        "draft": 2,
        "submitted": 1,
        "approved": 2,
        "rejected": 1,
    }
    assert body["review"] == {"approved": 2, "rejected": 1, "rejection_rate": pytest.approx(1 / 3)}

    # Class balance over latest non-draft versions: a(car x2), b(bus + weather), c(car), d(bus).
    labels = {row["label"]: row["count"] for row in body["classes"]}
    assert labels == {"car": 3, "bus": 2, "weather: rain": 1}
    assert [row["label"] for row in body["classes"]] == ["car", "bus", "weather: rain"]

    assert body["annotators"] == [
        {
            "user_id": str(seed.alice),
            "display_name": "Alice",
            "submitted": 1,
            "approved": 1,
            "rejected": 0,
        },
        {
            "user_id": str(seed.bob),
            "display_name": "Bob",
            "submitted": 0,
            "approved": 1,
            "rejected": 1,
        },
    ]


async def test_throughput_is_zero_filled_and_windowed(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(sessionmaker)
    _sign_in(app, seed.alice, seed.organization_id)

    response = client.get(f"/api/v1/projects/{seed.project_id}/stats", params={"days": 7})

    assert response.status_code == 200, response.text
    series = response.json()["throughput"]
    assert len(series) == 7
    today = datetime.now(UTC).date()
    assert series[-1]["day"] == today.isoformat()
    assert series[0]["day"] == (today - timedelta(days=6)).isoformat()
    # Today: b approved, c rejected, d submitted. Yesterday: a v2 approved. 3 days ago:
    # a v1 rejected. The model draft 40 days ago is outside the window.
    assert series[-1] == {"day": today.isoformat(), "submitted": 1, "approved": 1, "rejected": 1}
    assert series[-2]["approved"] == 1
    assert series[-4]["rejected"] == 1
    assert sum(row["submitted"] + row["approved"] + row["rejected"] for row in series) == 5


async def test_days_out_of_range_is_422(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(sessionmaker)
    _sign_in(app, seed.alice, seed.organization_id)

    assert client.get(f"/api/v1/projects/{seed.project_id}/stats?days=0").status_code == 422
    assert client.get(f"/api/v1/projects/{seed.project_id}/stats?days=91").status_code == 422


async def test_non_member_is_forbidden(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(sessionmaker)
    _sign_in(app, seed.bob, seed.organization_id)  # in the organisation, not in the project

    assert client.get(f"/api/v1/projects/{seed.project_id}/stats").status_code == 403


async def test_empty_project_returns_zeros(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await _seed(sessionmaker)
    async with sessionmaker() as session:
        empty = Project(organization_id=seed.organization_id, name="Empty")
        session.add(empty)
        await session.flush()
        session.add(Membership(user_id=seed.alice, project_id=empty.id, role=ProjectRole.OWNER))
        await session.commit()
        empty_id = empty.id
    _sign_in(app, seed.alice, seed.organization_id)

    body = client.get(f"/api/v1/projects/{empty_id}/stats").json()

    assert body["items"]["total"] == 0
    assert body["annotations"] == {
        "versions": 0,
        "by_source": {},
        "latest_by_status": {
            "draft": 0,
            "submitted": 0,
            "approved": 0,
            "rejected": 0,
        },
    }
    assert body["review"] == {"approved": 0, "rejected": 0, "rejection_rate": 0.0}
    assert body["classes"] == []
    assert body["annotators"] == []
    assert len(body["throughput"]) == 14
