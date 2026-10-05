"""Quality control endpoints: consensus, gold, agreement (QA-1 … QA-4).

CONTRACTS.md *Quality control* and the REST rows under `/items/{id}/consensus`,
`/items/{id}/gold`, `/projects/{id}/gold/tasks`, `/projects/{id}/agreement` and
`/projects/{id}/quality/annotators`. Same SQLite setup as
`tests/test_api_annotations.py`, plus the `user` table — these responses name
the annotators.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    ItemStatus,
    OutboxEvent,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.schemas import AnnotationResult
from app.services.annotations import create_version
from tests.test_api_annotations import (
    _TABLES,
    _add_item,
    _add_member,
    _add_task,
    _bbox,
    _make_user,
    _result,
    _seed_project,
    _set_workflow,
    _sign_in,
    _tasks_for_item,
)

Maker = async_sessionmaker[AsyncSession]


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


@pytest.fixture
async def sessionmaker() -> AsyncIterator[Maker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=[*_TABLES, cast(Table, User.__table__)]
        )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def app(sessionmaker: Maker) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class _World:
    """A project with an owner-reviewer, annotators, and one image item."""

    def __init__(self, org_id: UUID, connector_id: UUID, project_id: UUID, schema: UUID) -> None:
        self.org_id = org_id
        self.connector_id = connector_id
        self.project_id = project_id
        self.schema_version_id = schema

    async def person(self, sessionmaker: Maker, role: ProjectRole, name: str) -> CurrentUser:
        user = _make_user(self.org_id)
        async with sessionmaker() as session:
            session.add(
                User(
                    id=user.id,
                    organization_id=self.org_id,
                    email=f"{name}@example.com",
                    display_name=name.title(),
                    is_active=True,
                    is_superuser=False,
                )
            )
            await session.commit()
        await _add_member(sessionmaker, self.project_id, user.id, role)
        return user


async def _world(sessionmaker: Maker, workflow: dict[str, Any] | None = None) -> _World:
    world = _World(*await _seed_project(sessionmaker))
    await _set_workflow(sessionmaker, world.project_id, workflow or {"consensus_annotators": 2})
    return world


async def _version(
    sessionmaker: Maker,
    world: _World,
    item_id: UUID,
    author: UUID,
    *shapes: dict[str, Any],
    kind: AnnotationKind = AnnotationKind.CONSENSUS,
    status: AnnotationStatus = AnnotationStatus.SUBMITTED,
    classification: dict[str, Any] | None = None,
) -> UUID:
    result = _result(*shapes)
    result["classification"] = classification or {}
    async with sessionmaker() as session:
        item = await session.get(Item, item_id)
        assert item is not None
        annotation = await create_version(
            session,
            item=item,
            result=AnnotationResult.model_validate(result),
            label_schema_version_id=world.schema_version_id,
            author_user_id=author,
            status=status,
        )
        annotation.kind = kind
        await session.commit()
        return annotation.id


async def _consensus_item(
    sessionmaker: Maker, world: _World, authors: list[CurrentUser]
) -> tuple[UUID, list[UUID]]:
    """A submitted item with one review task and one consensus version per author."""
    item_id = await _add_item(
        sessionmaker,
        project_id=world.project_id,
        connector_id=world.connector_id,
        path=f"images/{uuid4().hex}.jpg",
        status=ItemStatus.SUBMITTED,
    )
    await _add_task(
        sessionmaker,
        project_id=world.project_id,
        item_id=item_id,
        task_type=TaskType.REVIEW,
        status=TaskStatus.OPEN,
    )
    versions = [
        await _version(
            sessionmaker,
            world,
            item_id,
            author.id,
            _bbox(coords=(10 + i, 10, 50 + i, 50)),
            classification={"weather": "rain"},
        )
        for i, author in enumerate(authors)
    ]
    return item_id, versions


class TestConsensusRead:
    async def test_reviewer_sees_versions_agreement_and_preview(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        ben = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "ben")
        item_id, _ = await _consensus_item(sessionmaker, world, [anna, ben])

        _sign_in(app, reviewer)
        response = client.get(f"/api/v1/items/{item_id}/consensus")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expected"] == 2
        assert {a["email"] for a in body["annotators"]} == {"anna@example.com", "ben@example.com"}
        assert body["agreement"]["shapes"]["f1"] == 1.0
        assert [s["type"] for s in body["preview"]["shapes"]] == ["bbox"]
        assert body["preview"]["classification"] == {"weather": "rain"}
        assert body["conflicts"] == []

        _sign_in(app, anna)
        assert client.get(f"/api/v1/items/{item_id}/consensus").status_code == 403

    async def test_item_without_consensus_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        item_id = await _add_item(
            sessionmaker, project_id=world.project_id, connector_id=world.connector_id
        )
        _sign_in(app, reviewer)
        assert client.get(f"/api/v1/items/{item_id}/consensus").status_code == 409


class TestResolve:
    async def test_fuse_approves_a_new_primary_version(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        ben = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "ben")
        item_id, _ = await _consensus_item(sessionmaker, world, [anna, ben])

        _sign_in(app, reviewer)
        response = client.post(
            f"/api/v1/items/{item_id}/consensus/resolve", json={"method": "fuse"}
        )
        assert response.status_code in (200, 201), response.text
        annotation_id = UUID(response.json()["id"])

        async with sessionmaker() as session:
            stored = await session.get(Annotation, annotation_id)
            assert stored is not None
            assert stored.kind is AnnotationKind.PRIMARY
            assert stored.status is AnnotationStatus.APPROVED
            assert stored.author_user_id == reviewer.id
            assert stored.result["shapes"][0]["bbox"] == [10.5, 10, 50.5, 50]
            item = await session.get(Item, item_id)
            assert item is not None and item.status is ItemStatus.APPROVED
            published = await session.scalar(select(func.count()).select_from(OutboxEvent))
            assert published is not None and published >= 1
        reviews = await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW)
        assert [t.status for t in reviews] == [TaskStatus.DONE]

    async def test_pick_copies_one_annotators_result(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        ben = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "ben")
        item_id, (_, bens) = await _consensus_item(sessionmaker, world, [anna, ben])

        _sign_in(app, reviewer)
        response = client.post(
            f"/api/v1/items/{item_id}/consensus/resolve",
            json={"method": "pick", "annotation_id": str(bens)},
        )
        assert response.status_code in (200, 201), response.text
        assert response.json()["result"]["shapes"][0]["bbox"] == [11, 10, 51, 50]

    async def test_resolve_needs_a_submitted_item(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        item_id, _ = await _consensus_item(sessionmaker, world, [anna])
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            item.status = ItemStatus.ANNOTATING
            await session.commit()
        _sign_in(app, reviewer)
        response = client.post(
            f"/api/v1/items/{item_id}/consensus/resolve", json={"method": "fuse"}
        )
        assert response.status_code == 409

    async def test_self_review_rule_covers_consensus_authors(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker, {"consensus_annotators": 2, "allow_self_review": False})
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        item_id, _ = await _consensus_item(sessionmaker, world, [anna, reviewer])
        _sign_in(app, reviewer)
        response = client.post(
            f"/api/v1/items/{item_id}/consensus/resolve", json={"method": "fuse"}
        )
        assert response.status_code == 403


class TestGold:
    async def test_set_open_score_and_clear(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker, {})
        owner = await world.person(sessionmaker, ProjectRole.OWNER, "olga")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        await world.person(sessionmaker, ProjectRole.ANNOTATOR, "ben")
        item_id = await _add_item(
            sessionmaker,
            project_id=world.project_id,
            connector_id=world.connector_id,
            status=ItemStatus.APPROVED,
        )
        reference_box = _bbox(coords=(10, 10, 50, 50))
        submitted = await _version(
            sessionmaker, world, item_id, owner.id, reference_box, kind=AnnotationKind.PRIMARY
        )
        approved = await _version(
            sessionmaker,
            world,
            item_id,
            owner.id,
            reference_box,
            kind=AnnotationKind.PRIMARY,
            status=AnnotationStatus.APPROVED,
        )

        _sign_in(app, owner)
        gold_url = f"/api/v1/items/{item_id}/gold"
        assert client.put(gold_url, json={"annotation_id": str(submitted)}).status_code == 422
        response = client.put(gold_url, json={"annotation_id": str(approved)})
        assert response.status_code == 200, response.text
        assert response.json()["meta"]["gold_annotation_id"] == str(approved)

        tasks_url = f"/api/v1/projects/{world.project_id}/gold/tasks"
        opened = client.post(tasks_url, json={})
        assert opened.status_code == 200, opened.text
        assert opened.json()["opened"] == 2
        assert client.post(tasks_url, json={}).json()["opened"] == 0

        # Anna's attempt matches the reference exactly; Ben has not submitted.
        await _version(
            sessionmaker, world, item_id, anna.id, reference_box, kind=AnnotationKind.GOLD
        )
        quality = client.get(f"/api/v1/projects/{world.project_id}/quality/annotators")
        assert quality.status_code == 200, quality.text
        by_email = {row["email"]: row for row in quality.json()["annotators"]}
        assert by_email["anna@example.com"]["shape_f1"] == 1.0
        assert by_email["anna@example.com"]["score"] == 1.0

        assert client.delete(gold_url).status_code == 200
        async with sessionmaker() as session:
            live = await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.item_id == item_id,
                    Task.gold.is_(True),
                    Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
                )
            )
            assert live == 0

    async def test_annotators_cannot_manage_gold(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker, {})
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        _sign_in(app, anna)
        url = f"/api/v1/projects/{world.project_id}"
        assert client.post(f"{url}/gold/tasks", json={}).status_code == 403
        assert client.get(f"{url}/quality/annotators").status_code == 403
        assert client.get(f"{url}/agreement").status_code == 403


class TestProjectAgreement:
    async def test_pools_items_with_two_or_more_versions(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        world = await _world(sessionmaker)
        reviewer = await world.person(sessionmaker, ProjectRole.REVIEWER, "rita")
        anna = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "anna")
        ben = await world.person(sessionmaker, ProjectRole.ANNOTATOR, "ben")
        await _consensus_item(sessionmaker, world, [anna, ben])
        await _consensus_item(sessionmaker, world, [anna])  # one version: not counted

        _sign_in(app, reviewer)
        response = client.get(f"/api/v1/projects/{world.project_id}/agreement")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"] == 1
        assert body["shapes"]["envelope_iou"] is True
        assert len(body["pairs"]) == 1
