"""Tests for `api/v1/annotations.py`.

Same infrastructure as `tests/test_api_items.py` and `tests/test_api_tasks.py`:
an in-memory SQLite database per test (`StaticPool` keeps one connection alive
for the whole test), wired in through `app.dependency_overrides`, with no live
database and no `user` table (see `test_api_items.py`'s module docstring for
why). This module additionally needs `label_schema`, `label_schema_version`
and `outbox_event` — `create_version` (the shared plumbing behind every
endpoint here) writes an outbox row in the same transaction as the annotation
(DATA-2), and validates a submitted result against the schema version (QA-6),
so a real, valid schema version is required. `DEMO_SCHEMA` (`app.demo`) is
reused as that schema, exactly as `tests/test_worker.py` does, since it
already defines a `car` class with a `bbox` tool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.demo import DEMO_SCHEMA
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationStatus,
    AuditEvent,
    Comment,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    ItemStatus,
    LabelSchema,
    LabelSchemaVersion,
    MediaType,
    Membership,
    Notification,
    Organization,
    OutboxEvent,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    Webhook,
    WebhookDelivery,
)
from app.schemas import AnnotationResult
from app.services.annotations import create_version
from app.services.webhooks import seal_secret

_TABLES: list[Table] = [
    cast(Table, AuditEvent.__table__),
    cast(Table, Organization.__table__),
    cast(Table, Connector.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, Item.__table__),
    cast(Table, Task.__table__),
    cast(Table, Annotation.__table__),
    cast(Table, LabelSchema.__table__),
    cast(Table, LabelSchemaVersion.__table__),
    cast(Table, OutboxEvent.__table__),
    cast(Table, Comment.__table__),
    cast(Table, Notification.__table__),
    cast(Table, Webhook.__table__),
    cast(Table, WebhookDelivery.__table__),
]


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


def _sign_in(app: FastAPI, user: CurrentUser) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _make_user(organization_id: UUID, *, is_superuser: bool = False) -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=organization_id,
        email="person@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )


def _result(*shapes: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "media_type": "image",
        "classification": {},
        "shapes": list(shapes),
    }


def _bbox(cls: str = "car", coords: tuple[float, ...] = (1, 2, 30, 40)) -> dict[str, Any]:
    return {"id": str(uuid4()), "type": "bbox", "class": cls, "bbox": list(coords)}


async def _seed_project(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID]:
    """Insert an organization, a connector, a project and a label schema version.

    Returns (organization_id, connector_id, project_id, label_schema_version_id).
    """
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()

        connector = Connector(
            organization_id=org.id,
            name="source",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp/fixture"},
        )
        session.add(connector)
        await session.flush()

        project = Project(organization_id=org.id, name="Project 1")
        session.add(project)
        await session.flush()

        schema = LabelSchema(project_id=project.id, name="traffic")
        session.add(schema)
        await session.flush()

        schema_version = LabelSchemaVersion(
            label_schema_id=schema.id, version=1, definition=DEMO_SCHEMA
        )
        session.add(schema_version)
        await session.flush()

        await session.commit()
        return org.id, connector.id, project.id, schema_version.id


async def _add_member(
    sessionmaker: async_sessionmaker[AsyncSession],
    project_id: UUID,
    user_id: UUID,
    role: ProjectRole,
) -> None:
    async with sessionmaker() as session:
        session.add(Membership(user_id=user_id, project_id=project_id, role=role))
        await session.commit()


async def _add_item(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    connector_id: UUID,
    path: str = "images/a.jpg",
    status: ItemStatus = ItemStatus.ANNOTATING,
) -> UUID:
    async with sessionmaker() as session:
        item = Item(
            project_id=project_id,
            connector_id=connector_id,
            path=path,
            media_type=MediaType.IMAGE,
            size_bytes=100,
            meta={},
            status=status,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item.id


_DEADLINE = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


async def _add_task(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    item_id: UUID,
    task_type: TaskType = TaskType.ANNOTATE,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    assignee_id: UUID | None = None,
    locked_by_id: UUID | None = None,
    priority: int = 0,
    deadline: datetime | None = None,
) -> UUID:
    async with sessionmaker() as session:
        task = Task(
            project_id=project_id,
            item_id=item_id,
            type=task_type,
            status=status,
            assignee_id=assignee_id,
            locked_by_id=locked_by_id,
            priority=priority,
            deadline=deadline,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _task_row(sessionmaker: async_sessionmaker[AsyncSession], task_id: UUID) -> Task:
    async with sessionmaker() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task


async def _tasks_for_item(
    sessionmaker: async_sessionmaker[AsyncSession], item_id: UUID, task_type: TaskType
) -> list[Task]:
    from sqlalchemy import select

    async with sessionmaker() as session:
        rows = await session.scalars(
            select(Task).where(Task.item_id == item_id, Task.type == task_type)
        )
        return list(rows)


async def _add_annotation(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    item_id: UUID,
    schema_version_id: UUID,
    author_user_id: UUID,
    status: AnnotationStatus = AnnotationStatus.SUBMITTED,
    task_id: UUID | None = None,
) -> UUID:
    async with sessionmaker() as session:
        item = await session.get(Item, item_id)
        assert item is not None
        annotation = await create_version(
            session,
            item=item,
            result=AnnotationResult.model_validate(_result(_bbox())),
            label_schema_version_id=schema_version_id,
            author_user_id=author_user_id,
            task_id=task_id,
            status=status,
        )
        await session.commit()
        await session.refresh(annotation)
        return annotation.id


class TestSubmitViaCreateAnnotation:
    async def test_submit_completes_annotate_task_and_opens_review_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            task_type=TaskType.ANNOTATE,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=user.id,
            locked_by_id=user.id,
            priority=4,
            deadline=_DEADLINE,
        )
        _sign_in(app, user)

        response = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "task_id": str(task_id),
                "submit": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["status"] == "submitted"

        annotate_task = await _task_row(sessionmaker, task_id)
        assert annotate_task.status is TaskStatus.DONE
        assert annotate_task.locked_by_id is None
        assert annotate_task.locked_until is None

        review_tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW)
        assert len(review_tasks) == 1
        # The review inherits the annotate task's urgency (WF-6).
        assert review_tasks[0].priority == 4
        assert review_tasks[0].deadline is not None
        assert review_tasks[0].deadline.replace(tzinfo=UTC) == _DEADLINE  # SQLite drops tz
        assert review_tasks[0].status is TaskStatus.OPEN

    async def test_one_click_submit_on_a_new_item_succeeds(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A freshly scanned item is `new`; submitting without a prior draft must work."""
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )
        _sign_in(app, user)

        response = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "submit": True,
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["status"] == "submitted"
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
        assert item is not None
        assert item.status is ItemStatus.SUBMITTED


class TestSubmitDraft:
    async def test_submit_endpoint_completes_annotate_task_and_opens_review_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            task_type=TaskType.ANNOTATE,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=user.id,
            locked_by_id=user.id,
        )
        draft_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=user.id,
            status=AnnotationStatus.DRAFT,
            task_id=task_id,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/annotations/{draft_id}/submit")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "submitted"
        assert body["version"] == 2  # a new version, the draft is untouched

        annotate_task = await _task_row(sessionmaker, task_id)
        assert annotate_task.status is TaskStatus.DONE
        assert annotate_task.locked_by_id is None

        review_tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW)
        assert len(review_tasks) == 1
        assert review_tasks[0].status is TaskStatus.OPEN


class TestReviewAnnotation:
    async def test_approve_completes_review_task_and_opens_no_annotate_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        author_id = uuid4()
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        review_task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            task_type=TaskType.REVIEW,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=reviewer.id,
            locked_by_id=reviewer.id,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=author_id,
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": True}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"

        review_task = await _task_row(sessionmaker, review_task_id)
        assert review_task.status is TaskStatus.DONE
        assert review_task.locked_by_id is None

        annotate_tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert annotate_tasks == []

    async def test_in_place_verdict_emits_an_outbox_event_for_the_same_blob(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Review changes `status` in place, so the blob copy must be re-published
        with the verdict — otherwise a rebuild from blobs (DATA-6) says `submitted`."""
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=uuid4(),
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": False}
        )
        assert response.status_code == 200

        async with sessionmaker() as session:
            events = list(
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.aggregate_id == annotation_id)
                    .order_by(OutboxEvent.created_at)
                )
            )
        assert len(events) == 2
        original, verdict = events
        assert verdict.type == original.type
        assert verdict.payload["blob_path"] == original.payload["blob_path"]
        assert original.payload["document"]["status"] == "submitted"
        assert verdict.payload["document"]["status"] == "rejected"
        assert verdict.payload["document"]["reviewer_id"] == str(reviewer.id)
        assert verdict.payload["document"]["version"] == original.payload["document"]["version"]

    async def test_reject_completes_review_task_and_reopens_annotate_task_for_author(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        author_id = uuid4()
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        review_task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            task_type=TaskType.REVIEW,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=reviewer.id,
            locked_by_id=reviewer.id,
            priority=3,
            deadline=_DEADLINE,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=author_id,
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": False}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"

        review_task = await _task_row(sessionmaker, review_task_id)
        assert review_task.status is TaskStatus.DONE

        annotate_tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert len(annotate_tasks) == 1
        assert annotate_tasks[0].status is TaskStatus.OPEN
        assert annotate_tasks[0].assignee_id == author_id
        # A rejected item keeps its place in the queue (WF-6).
        assert annotate_tasks[0].priority == 3
        assert annotate_tasks[0].deadline is not None
        assert annotate_tasks[0].deadline.replace(tzinfo=UTC) == _DEADLINE  # SQLite drops tz

    async def test_comment_is_stored_on_the_reviewed_version(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=uuid4(),
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review",
            json={"approve": False, "comment": "Box misses the trailer."},
        )
        assert response.status_code == 200

        async with sessionmaker() as session:
            comments = list(
                await session.scalars(select(Comment).where(Comment.annotation_id == annotation_id))
            )
        assert len(comments) == 1
        assert comments[0].body == "Box misses the trailer."
        assert comments[0].author_id == reviewer.id
        assert comments[0].item_id == item_id
        assert comments[0].project_id == project_id

    async def test_no_comment_row_without_a_comment(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=uuid4(),
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": True}
        )
        assert response.status_code == 200

        async with sessionmaker() as session:
            count = len(list(await session.scalars(select(Comment))))
        assert count == 0


async def _set_workflow(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: UUID, workflow: dict[str, Any]
) -> None:
    async with sessionmaker() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        project.workflow = workflow
        await session.commit()


class TestWebhookEvents:
    """Submit and review queue `annotation.*` deliveries for subscribers (API-4)."""

    async def _subscribe(
        self, sessionmaker: async_sessionmaker[AsyncSession], org_id: UUID, project_id: UUID
    ) -> None:
        async with sessionmaker() as session:
            session.add(
                Webhook(
                    organization_id=org_id,
                    project_id=project_id,
                    url="https://hooks.example/in",
                    events=["*"],
                    secret=seal_secret("s" * 64),
                )
            )
            await session.commit()

    async def _events(self, sessionmaker: async_sessionmaker[AsyncSession]) -> list[Any]:
        async with sessionmaker() as session:
            rows = await session.scalars(
                select(WebhookDelivery).order_by(WebhookDelivery.created_at)
            )
            return [(row.event, row.payload["data"]) for row in rows]

    async def test_submit_and_verdict_each_queue_one_event(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await self._subscribe(sessionmaker, org_id, project_id)
        annotator = _make_user(org_id)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, annotator.id, ProjectRole.ANNOTATOR)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )

        _sign_in(app, annotator)
        created = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "submit": True,
            },
        )
        assert created.status_code == 201
        annotation_id = created.json()["id"]

        _sign_in(app, reviewer)
        verdict = client.post(
            f"/api/v1/annotations/{annotation_id}/review",
            json={"approve": False, "comment": "redo"},
        )
        assert verdict.status_code == 200

        events = await self._events(sessionmaker)
        assert [name for name, _ in events] == ["annotation.submitted", "annotation.rejected"]
        submitted, rejected = (data for _, data in events)
        assert submitted["annotation_id"] == annotation_id
        assert submitted["item_status"] == "submitted"
        assert submitted["author_user_id"] == str(annotator.id)
        assert rejected["reviewer_id"] == str(reviewer.id)
        assert rejected["comment"] == "redo"
        assert rejected["corrected"] is False
        assert rejected["item_status"] == "rejected"

    async def test_item_approved_follows_an_approving_verdict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await self._subscribe(sessionmaker, org_id, project_id)
        annotator = _make_user(org_id)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, annotator.id, ProjectRole.ANNOTATOR)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )
        _sign_in(app, annotator)
        annotation_id = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "submit": True,
            },
        ).json()["id"]
        _sign_in(app, reviewer)
        verdict = client.post(f"/api/v1/annotations/{annotation_id}/review", json={"approve": True})
        assert verdict.status_code == 200

        events = await self._events(sessionmaker)
        assert [name for name, _ in events] == [
            "annotation.submitted",
            "annotation.approved",
            "item.approved",
        ]
        done = events[-1][1]
        assert done["item_id"] == str(item_id)
        assert done["via"] == "review"

    async def test_item_approved_on_a_submit_that_needs_no_review(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await _set_workflow(sessionmaker, project_id, {"review": "none"})
        await self._subscribe(sessionmaker, org_id, project_id)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )
        _sign_in(app, user)
        created = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "submit": True,
            },
        )
        assert created.status_code == 201

        events = await self._events(sessionmaker)
        assert [name for name, _ in events] == ["annotation.submitted", "item.approved"]
        assert events[-1][1]["via"] == "no_review"
        assert events[-1][1]["annotation_id"] == created.json()["id"]


class TestProjectWorkflow:
    """`project.workflow` changes what submit and review do (WF-1)."""

    async def test_review_none_approves_on_submit_without_a_review_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await _set_workflow(sessionmaker, project_id, {"review": "none"})
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            task_type=TaskType.ANNOTATE,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=user.id,
            locked_by_id=user.id,
        )
        _sign_in(app, user)

        response = client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": _result(_bbox()),
                "label_schema_version_id": str(schema_version_id),
                "task_id": str(task_id),
                "submit": True,
            },
        )
        assert response.status_code == 201

        item = client.get(f"/api/v1/items/{item_id}").json()
        assert item["status"] == "approved"
        assert (await _task_row(sessionmaker, task_id)).status is TaskStatus.DONE
        assert await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW) == []

    async def test_rejection_returns_to_queue_opens_an_unassigned_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await _set_workflow(sessionmaker, project_id, {"rejection_returns_to": "queue"})
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=uuid4(),
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": False}
        )
        assert response.status_code == 200

        annotate_tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert len(annotate_tasks) == 1
        assert annotate_tasks[0].status is TaskStatus.OPEN
        assert annotate_tasks[0].assignee_id is None

    async def test_self_review_refused_when_the_project_forbids_it(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        await _set_workflow(sessionmaker, project_id, {"allow_self_review": False})
        owner = _make_user(org_id, is_superuser=True)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=owner.id,
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": True}
        )
        assert response.status_code == 403
        assert "your own" in response.json()["detail"]
        item = client.get(f"/api/v1/items/{item_id}").json()
        assert item["status"] == "submitted"

        # Someone else may still review it.
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        _sign_in(app, reviewer)
        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review", json={"approve": True}
        )
        assert response.status_code == 200


class TestListAnnotations:
    async def test_returns_a_plain_array_newest_version_first(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=user.id,
            status=AnnotationStatus.SUBMITTED,
        )
        await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=schema_version_id,
            author_user_id=user.id,
            status=AnnotationStatus.SUBMITTED,
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/items/{item_id}/annotations")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert [entry["version"] for entry in body] == [2, 1]
