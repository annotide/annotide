"""Tests for `api/v1/tasks.py`.

Same infrastructure as `tests/test_api_items.py`: an in-memory SQLite
database per test (`StaticPool` keeps one connection alive for the whole
test), wired in through `app.dependency_overrides`, with no live database and
no `user` table (see that file's module docstring for why).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
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
from app.main import create_app
from app.models import (
    Annotation,
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    MediaType,
    Membership,
    Organization,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)

_TABLES: list[Table] = [
    cast(Table, AuditEvent.__table__),
    cast(Table, Organization.__table__),
    cast(Table, Connector.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, Item.__table__),
    cast(Table, Task.__table__),
    # `claim_next_task`'s slot-conflict check reads `annotation` (QA-1).
    cast(Table, Annotation.__table__),
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


def _sign_out(app: FastAPI) -> None:
    app.dependency_overrides.pop(get_current_user, None)


def _make_user(organization_id: UUID, *, is_superuser: bool = False) -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=organization_id,
        email="person@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )


async def _seed_project(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, UUID]:
    """Insert an organization, a connector and a project. Returns their ids."""
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

        await session.commit()
        return org.id, connector.id, project.id


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
    sessionmaker: async_sessionmaker[AsyncSession], *, project_id: UUID, connector_id: UUID
) -> UUID:
    async with sessionmaker() as session:
        item = Item(
            project_id=project_id,
            connector_id=connector_id,
            path=f"images/{uuid4().hex[:8]}.jpg",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            meta={},
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item.id


async def _add_task(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    item_id: UUID,
    task_type: TaskType = TaskType.ANNOTATE,
    status: TaskStatus = TaskStatus.OPEN,
    assignee_id: UUID | None = None,
    locked_by_id: UUID | None = None,
    locked_until: datetime | None = None,
    priority: int = 0,
) -> UUID:
    async with sessionmaker() as session:
        task = Task(
            project_id=project_id,
            item_id=item_id,
            type=task_type,
            status=status,
            assignee_id=assignee_id,
            locked_by_id=locked_by_id,
            locked_until=locked_until,
            priority=priority,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


class TestListTasks:
    async def test_requires_authentication(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, _, project_id = await _seed_project(sessionmaker)
        _sign_out(app)

        response = client.get(f"/api/v1/projects/{project_id}/tasks")

        assert response.status_code == 401

    async def test_cross_organisation_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, _, project_id = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(uuid4()))

        response = client.get(f"/api/v1/projects/{project_id}/tasks")

        assert response.status_code == 404

    async def test_non_member_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, project_id = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(org_id))

        response = client.get(f"/api/v1/projects/{project_id}/tasks")

        assert response.status_code == 403

    async def test_lists_and_filters_by_status_type_and_assignee(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_a = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        item_b = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        item_c = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        someone = uuid4()
        await _add_task(sessionmaker, project_id=project_id, item_id=item_a, status=TaskStatus.OPEN)
        await _add_task(sessionmaker, project_id=project_id, item_id=item_b, status=TaskStatus.DONE)
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_c,
            task_type=TaskType.REVIEW,
            assignee_id=someone,
        )
        _sign_in(app, user)

        by_status = client.get(
            f"/api/v1/projects/{project_id}/tasks", params={"status": "done"}
        ).json()
        assert len(by_status["items"]) == 1
        assert by_status["items"][0]["status"] == "done"

        by_type = client.get(
            f"/api/v1/projects/{project_id}/tasks", params={"type": "review"}
        ).json()
        assert len(by_type["items"]) == 1
        assert by_type["items"][0]["type"] == "review"

        by_assignee = client.get(
            f"/api/v1/projects/{project_id}/tasks", params={"assignee_id": str(someone)}
        ).json()
        assert len(by_assignee["items"]) == 1
        assert by_assignee["items"][0]["assignee_id"] == str(someone)

        everything = client.get(f"/api/v1/projects/{project_id}/tasks").json()
        assert len(everything["items"]) == 3


class TestCreateTask:
    async def test_owner_may_create(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(item_id), "type": "annotate", "priority": 5},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["item_id"] == str(item_id)
        assert body["priority"] == 5
        assert body["status"] == "open"

    async def test_foreign_project_item_is_not_found_and_creates_nothing(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, project_id = await _seed_project(sessionmaker)
        _, other_connector, other_project = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        foreign_item = await _add_item(
            sessionmaker, project_id=other_project, connector_id=other_connector
        )
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(foreign_item), "type": "annotate"},
        )

        assert response.status_code == 404
        async with sessionmaker() as session:
            assert (await session.scalars(select(Task))).all() == []

    async def test_nonexistent_item_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, project_id = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(uuid4()), "type": "annotate"},
        )

        assert response.status_code == 404

    async def test_non_member_assignee_is_rejected_and_member_accepted(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, owner)
        body = {"item_id": str(item_id), "type": "annotate"}

        rejected = client.post(
            f"/api/v1/projects/{project_id}/tasks", json={**body, "assignee_id": str(uuid4())}
        )
        assert rejected.status_code == 422
        async with sessionmaker() as session:
            assert (await session.scalars(select(Task))).all() == []

        member = uuid4()
        await _add_member(sessionmaker, project_id, member, ProjectRole.ANNOTATOR)
        accepted = client.post(
            f"/api/v1/projects/{project_id}/tasks", json={**body, "assignee_id": str(member)}
        )
        assert accepted.status_code == 201
        assert accepted.json()["assignee_id"] == str(member)

    async def test_annotator_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        annotator = _make_user(org_id)
        await _add_member(sessionmaker, project_id, annotator.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, annotator)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(item_id), "type": "annotate"},
        )

        assert response.status_code == 403

    async def test_reviewer_may_create(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        reviewer = _make_user(org_id)
        await _add_member(sessionmaker, project_id, reviewer.id, ProjectRole.REVIEWER)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, reviewer)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(item_id), "type": "review"},
        )

        assert response.status_code == 201

    async def test_deadline_is_stored(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={
                "item_id": str(item_id),
                "type": "annotate",
                "deadline": "2030-01-02T03:04:05Z",
            },
        )

        assert response.status_code == 201
        assert response.json()["deadline"].startswith("2030-01-02T03:04:05")

    async def test_second_live_task_of_same_type_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """One live task per item and type (WF-2): PATCH the existing one instead."""
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        owner = _make_user(org_id)
        await _add_member(sessionmaker, project_id, owner.id, ProjectRole.OWNER)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(sessionmaker, project_id=project_id, item_id=item_id)
        _sign_in(app, owner)

        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(item_id), "type": "annotate"},
        )

        assert response.status_code == 409
        # A different type on the same item is fine.
        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"item_id": str(item_id), "type": "review"},
        )
        assert response.status_code == 201


class TestPatchTask:
    async def _owner_with_task(
        self,
        app: FastAPI,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        role: ProjectRole = ProjectRole.OWNER,
        status: TaskStatus = TaskStatus.OPEN,
        locked_by_id: UUID | None = None,
    ) -> tuple[CurrentUser, UUID]:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, role)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=status,
            assignee_id=locked_by_id,
            locked_by_id=locked_by_id,
            priority=1,
        )
        _sign_in(app, user)
        return user, task_id

    async def test_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(org_id))

        response = client.patch(f"/api/v1/tasks/{uuid4()}", json={"priority": 3})

        assert response.status_code == 404

    async def test_owner_sets_priority_and_deadline(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker)

        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            json={"priority": 7, "deadline": "2030-06-01T12:00:00Z"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["priority"] == 7
        assert body["deadline"].startswith("2030-06-01T12:00:00")
        assert body["status"] == "open"

    async def test_only_present_keys_change_and_null_clears(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker)
        client.patch(f"/api/v1/tasks/{task_id}", json={"deadline": "2030-06-01T12:00:00Z"})

        # Priority alone: the deadline stays.
        response = client.patch(f"/api/v1/tasks/{task_id}", json={"priority": 2})
        assert response.status_code == 200
        assert response.json()["priority"] == 2
        assert response.json()["deadline"] is not None

        # Explicit null clears it.
        response = client.patch(f"/api/v1/tasks/{task_id}", json={"deadline": None})
        assert response.status_code == 200
        assert response.json()["deadline"] is None
        assert response.json()["priority"] == 2

    async def test_reviewer_may_reassign_open_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker, role=ProjectRole.REVIEWER)
        assignee = uuid4()
        async with sessionmaker() as session:
            project_id = (await session.scalars(select(Task.project_id))).one()
        await _add_member(sessionmaker, project_id, assignee, ProjectRole.ANNOTATOR)

        response = client.patch(f"/api/v1/tasks/{task_id}", json={"assignee_id": str(assignee)})

        assert response.status_code == 200
        assert response.json()["assignee_id"] == str(assignee)

    async def test_non_member_assignee_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker)

        response = client.patch(f"/api/v1/tasks/{task_id}", json={"assignee_id": str(uuid4())})

        assert response.status_code == 422
        async with sessionmaker() as session:
            assert (await session.scalars(select(Task.assignee_id))).one() is None

    async def test_annotator_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker, role=ProjectRole.ANNOTATOR)

        response = client.patch(f"/api/v1/tasks/{task_id}", json={"priority": 9})

        assert response.status_code == 403

    async def test_done_task_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, task_id = await self._owner_with_task(app, sessionmaker, status=TaskStatus.DONE)

        response = client.patch(f"/api/v1/tasks/{task_id}", json={"priority": 9})

        assert response.status_code == 409

    async def test_reassigning_in_progress_task_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        holder = uuid4()
        _, task_id = await self._owner_with_task(
            app, sessionmaker, status=TaskStatus.IN_PROGRESS, locked_by_id=holder
        )

        # Priority on an in-progress task is fine ...
        response = client.patch(f"/api/v1/tasks/{task_id}", json={"priority": 9})
        assert response.status_code == 200
        # ... and so is restating the current assignee ...
        response = client.patch(f"/api/v1/tasks/{task_id}", json={"assignee_id": str(holder)})
        assert response.status_code == 200
        # ... but handing it to someone else is not.
        someone_else = uuid4()
        async with sessionmaker() as session:
            project_id = (await session.scalars(select(Task.project_id))).one()
        await _add_member(sessionmaker, project_id, someone_else, ProjectRole.ANNOTATOR)
        response = client.patch(f"/api/v1/tasks/{task_id}", json={"assignee_id": str(someone_else)})
        assert response.status_code == 409


class TestClaimNext:
    async def test_empty_queue_returns_204(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        _sign_in(app, user)

        response = client.post("/api/v1/tasks/next", params={"project_id": str(project_id)})

        assert response.status_code == 204
        assert response.content == b""

    async def test_claims_highest_priority_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        low_item = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        high_item = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(sessionmaker, project_id=project_id, item_id=low_item, priority=1)
        high_task_id = await _add_task(
            sessionmaker, project_id=project_id, item_id=high_item, priority=10
        )
        _sign_in(app, user)

        response = client.post("/api/v1/tasks/next", params={"project_id": str(project_id)})

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(high_task_id)
        assert body["status"] == "in_progress"
        assert body["assignee_id"] == str(user.id)
        assert body["locked_until"] is not None

    async def test_project_id_from_body_is_accepted(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(sessionmaker, project_id=project_id, item_id=item_id)
        _sign_in(app, user)

        response = client.post("/api/v1/tasks/next", json={"project_id": str(project_id)})

        assert response.status_code == 200

    async def test_missing_project_id_is_a_bad_request(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(org_id))

        response = client.post("/api/v1/tasks/next")

        assert response.status_code == 400

    async def test_locked_task_is_skipped_for_another_claimant(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        locked_item = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id
        )
        open_item = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=locked_item,
            priority=10,
            locked_by_id=uuid4(),
            locked_until=datetime.now(UTC) + timedelta(minutes=30),
        )
        open_task_id = await _add_task(
            sessionmaker, project_id=project_id, item_id=open_item, priority=1
        )
        _sign_in(app, user)

        response = client.post("/api/v1/tasks/next", params={"project_id": str(project_id)})

        assert response.status_code == 200
        assert response.json()["id"] == str(open_task_id)

    async def test_type_query_param_filters_to_review(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        annotate_item = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id
        )
        review_item = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id
        )
        # Higher priority, but the wrong type: must not be the one claimed.
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=annotate_item,
            task_type=TaskType.ANNOTATE,
            priority=10,
        )
        review_task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=review_item,
            task_type=TaskType.REVIEW,
            priority=1,
        )
        _sign_in(app, user)

        response = client.post(
            "/api/v1/tasks/next",
            params={"project_id": str(project_id), "type": "review"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(review_task_id)
        assert body["type"] == "review"

    async def test_type_query_param_with_only_the_other_type_returns_204(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(
            sessionmaker, project_id=project_id, item_id=item_id, task_type=TaskType.ANNOTATE
        )
        _sign_in(app, user)

        response = client.post(
            "/api/v1/tasks/next",
            params={"project_id": str(project_id), "type": "review"},
        )

        assert response.status_code == 204
        assert response.content == b""

    async def test_type_from_body_is_accepted(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        review_task_id = await _add_task(
            sessionmaker, project_id=project_id, item_id=item_id, task_type=TaskType.REVIEW
        )
        _sign_in(app, user)

        response = client.post(
            "/api/v1/tasks/next",
            json={"project_id": str(project_id), "type": "review"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == str(review_task_id)

    async def test_query_type_wins_over_body_type(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        annotate_task_id = await _add_task(
            sessionmaker, project_id=project_id, item_id=item_id, task_type=TaskType.ANNOTATE
        )
        _sign_in(app, user)

        # Query says annotate, body says review; the queue only has an
        # annotate task, so this only succeeds if the query parameter wins.
        response = client.post(
            "/api/v1/tasks/next",
            params={"project_id": str(project_id), "type": "annotate"},
            json={"project_id": str(project_id), "type": "review"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == str(annotate_task_id)


class TestReleaseTask:
    async def test_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(org_id))

        response = client.post(f"/api/v1/tasks/{uuid4()}/release")

        assert response.status_code == 404

    async def test_holder_releases(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=user.id,
            locked_by_id=user.id,
            locked_until=datetime.now(UTC) + timedelta(minutes=30),
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/tasks/{task_id}/release")

        assert response.status_code == 200
        body = response.json()
        assert body["locked_by_id"] is None
        assert body["status"] == "open"

    async def test_releasing_a_task_you_do_not_hold_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        holder_id = uuid4()
        other = _make_user(org_id)
        await _add_member(sessionmaker, project_id, other.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=holder_id,
            locked_by_id=holder_id,
            locked_until=datetime.now(UTC) + timedelta(minutes=30),
        )
        _sign_in(app, other)

        response = client.post(f"/api/v1/tasks/{task_id}/release")

        assert response.status_code == 409

    async def test_superuser_may_release_anyones_lock(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        holder_id = uuid4()
        superuser = _make_user(org_id, is_superuser=True)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=holder_id,
            locked_by_id=holder_id,
            locked_until=datetime.now(UTC) + timedelta(minutes=30),
        )
        _sign_in(app, superuser)

        response = client.post(f"/api/v1/tasks/{task_id}/release")

        assert response.status_code == 200
        assert response.json()["locked_by_id"] is None


class TestExtendTask:
    async def test_holder_extends(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        original_expiry = datetime.now(UTC) + timedelta(seconds=5)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=user.id,
            locked_by_id=user.id,
            locked_until=original_expiry,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/tasks/{task_id}/extend", json={"lock_ttl_seconds": 3600})

        assert response.status_code == 200
        new_expiry = datetime.fromisoformat(response.json()["locked_until"])
        # SQLite (this test's stand-in database) does not actually preserve
        # timezone offsets the way PostgreSQL's `timestamptz` does, so the
        # round-tripped value comes back naive; compare on the naive instant.
        assert new_expiry.replace(tzinfo=None) > original_expiry.replace(tzinfo=None)

    async def test_non_holder_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        holder_id = uuid4()
        other = _make_user(org_id)
        await _add_member(sessionmaker, project_id, other.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        task_id = await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=holder_id,
            locked_by_id=holder_id,
            locked_until=datetime.now(UTC) + timedelta(minutes=30),
        )
        _sign_in(app, other)

        response = client.post(f"/api/v1/tasks/{task_id}/extend")

        assert response.status_code == 409
