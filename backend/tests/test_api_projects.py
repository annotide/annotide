"""Tests for the `/projects` router.

No live database: each test gets a fresh in-memory SQLite database (via an
async engine on a `StaticPool`, so the same connection survives across the
requests a single test makes) with only the tables this router touches, and
`app.api.deps.get_current_user` is overridden with a fake `CurrentUser`
instead of decoding a real JWT.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    IdempotencyKey,
    Job,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Project,
    ProjectRole,
)
from app.services.queue import QueueUnavailableError, get_job_queue
from tests.support import FakeJobQueue

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
OWNER_ID = uuid.uuid4()
ANNOTATOR_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [
        AuditEvent.__table__,
        Connector.__table__,
        IdempotencyKey.__table__,
        Project.__table__,
        Job.__table__,
        LabelSchema.__table__,
        LabelSchemaVersion.__table__,
        Membership.__table__,
    ],
)

_VALID_SCHEMA_BODY = {
    "version": 1,
    "classes": [
        {
            "name": "car",
            "display_name": "Car",
            "color": "#e11d48",
            "tools": ["bbox"],
            "attributes": [],
        }
    ],
    "classification": [],
}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture
def engine() -> Iterator[Any]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TABLES)

    _run(_create())
    yield eng
    _run(eng.dispose())


@pytest.fixture
def sessionmaker(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


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


def _login(
    app: FastAPI,
    *,
    user_id: uuid.UUID = OWNER_ID,
    organization_id: uuid.UUID = ORG_ID,
    is_superuser: bool = False,
) -> None:
    user = CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="user@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed_project(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
    name: str = "proj",
    created_at: datetime | None = None,
    settings: dict[str, Any] | None = None,
    description: str | None = "a project",
) -> Project:
    moment = created_at or datetime.now(UTC)

    async def _create() -> Project:
        async with sessionmaker() as session:
            project = Project(
                organization_id=organization_id,
                name=name,
                description=description,
                settings=settings or {},
                workflow={},
                created_at=moment,
                updated_at=moment,
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            return project

    return _run(_create())


def _seed_membership(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID = OWNER_ID,
    role: ProjectRole = ProjectRole.OWNER,
) -> None:
    async def _create() -> None:
        async with sessionmaker() as session:
            session.add(Membership(user_id=user_id, project_id=project_id, role=role))
            await session.commit()

    _run(_create())


class TestList:
    def test_list_scoped_to_organization(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _seed_project(sessionmaker, organization_id=ORG_ID, name="mine")
        _seed_project(sessionmaker, organization_id=OTHER_ORG_ID, name="not-mine")
        _login(app, is_superuser=True)

        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        body = response.json()
        assert [item["name"] for item in body["items"]] == ["mine"]

    def test_cursor_pagination_round_trip(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            project = _seed_project(
                sessionmaker, name=f"p{i}", created_at=base + timedelta(seconds=i)
            )
            _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.VIEWER)
        _login(app)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            response = client.get("/api/v1/projects", params=params)
            assert response.status_code == 200
            body = response.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["name"])
            cursor = body["next_cursor"]

        assert seen == ["p2", "p1", "p0"]  # newest (created_at desc) first
        assert cursor is None

    def test_lists_only_the_callers_memberships(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        member = _seed_project(sessionmaker, name="member")
        _seed_membership(sessionmaker, project_id=member.id, role=ProjectRole.ANNOTATOR)
        stranger = _seed_project(sessionmaker, name="stranger")
        _seed_membership(sessionmaker, project_id=stranger.id, user_id=uuid.uuid4())
        _seed_project(sessionmaker, name="nobody")
        _login(app)

        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        assert [item["name"] for item in response.json()["items"]] == ["member"]
        # Consistent with the detail route: what the list hides, GET refuses.
        assert client.get(f"/api/v1/projects/{stranger.id}").status_code == 403

    def test_a_superuser_lists_every_project_of_the_organization(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        _seed_project(sessionmaker, name="a", created_at=base)
        _seed_project(sessionmaker, name="b", created_at=base + timedelta(seconds=1))
        _login(app, is_superuser=True)

        response = client.get("/api/v1/projects")
        assert [item["name"] for item in response.json()["items"]] == ["b", "a"]

    def test_401_without_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/projects")
        assert response.status_code == 401


def _seed_connector(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
    type_: ConnectorType = ConnectorType.LOCAL,
) -> uuid.UUID:
    async def _create() -> uuid.UUID:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=organization_id,
                name=f"{type_.value}-{uuid.uuid4().hex[:6]}",
                type=type_,
                identity_type=ConnectorIdentity.NONE,
                config={},
            )
            session.add(connector)
            await session.commit()
            return connector.id

    return _run(_create())


class TestConnectorGuard:
    """A project may only use its own organisation's connectors, and never write to `http`."""

    def test_create_with_own_connectors(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _login(app)
        source = _seed_connector(sessionmaker, type_=ConnectorType.HTTP)
        result = _seed_connector(sessionmaker)
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "p",
                "source_connector_id": str(source),
                "result_connector_id": str(result),
            },
        )
        assert response.status_code == 201, response.text

    def test_another_organisations_connector_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _login(app)
        foreign = _seed_connector(sessionmaker, organization_id=OTHER_ORG_ID)
        response = client.post(
            "/api/v1/projects", json={"name": "p", "source_connector_id": str(foreign)}
        )
        assert response.status_code == 404
        missing = client.post(
            "/api/v1/projects", json={"name": "p", "result_connector_id": str(uuid.uuid4())}
        )
        assert missing.status_code == 404

    def test_http_cannot_be_the_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _login(app)
        http = _seed_connector(sessionmaker, type_=ConnectorType.HTTP)
        created = client.post(
            "/api/v1/projects", json={"name": "p", "result_connector_id": str(http)}
        )
        assert created.status_code == 422

        project = client.post("/api/v1/projects", json={"name": "q"}).json()
        patched = client.patch(
            f"/api/v1/projects/{project['id']}", json={"result_connector_id": str(http)}
        )
        assert patched.status_code == 422
        foreign = _seed_connector(sessionmaker, organization_id=OTHER_ORG_ID)
        patched = client.patch(
            f"/api/v1/projects/{project['id']}", json={"source_connector_id": str(foreign)}
        )
        assert patched.status_code == 404


class TestCreate:
    def test_create_project(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        response = client.post("/api/v1/projects", json={"name": "new project"})
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "new project"
        assert body["organization_id"] == str(ORG_ID)

    def test_workflow_defaults_validation_and_update(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """`project.workflow` (WF-1): defaults filled in, unknown keys refused, PATCH replaces."""
        _login(app)
        created = client.post("/api/v1/projects", json={"name": "wf"})
        assert created.status_code == 201
        assert created.json()["workflow"] == {
            "review": "required",
            "rejection_returns_to": "same_annotator",
            "allow_skip": True,
            "allow_self_review": True,
            "consensus_annotators": 1,
            "review_sample_rate": 0.1,
            "gold_every": None,
        }
        project_id = created.json()["id"]

        bad = client.post("/api/v1/projects", json={"name": "x", "workflow": {"reviw": "none"}})
        assert bad.status_code == 422
        bad = client.post("/api/v1/projects", json={"name": "x", "workflow": {"review": "maybe"}})
        assert bad.status_code == 422

        updated = client.patch(
            f"/api/v1/projects/{project_id}",
            json={"workflow": {"review": "none", "allow_skip": False}},
        )
        assert updated.status_code == 200
        assert updated.json()["workflow"] == {
            "review": "none",
            "rejection_returns_to": "same_annotator",
            "allow_skip": False,
            "allow_self_review": True,
            "consensus_annotators": 1,
            "review_sample_rate": 0.1,
            "gold_every": None,
        }
        assert client.get(f"/api/v1/projects/{project_id}").json()["workflow"]["review"] == "none"

    def test_creator_becomes_owner(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        created = client.post("/api/v1/projects", json={"name": "mine"})
        project_id = created.json()["id"]

        # PATCH is owner-gated; the creator must pass without a seeded membership.
        response = client.patch(f"/api/v1/projects/{project_id}", json={"name": "renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "renamed"

    def test_idempotency_key_returns_existing_row(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        headers = {"Idempotency-Key": "retry-me"}

        first = client.post("/api/v1/projects", json={"name": "first try"}, headers=headers)
        assert first.status_code == 201
        first_id = first.json()["id"]

        second = client.post("/api/v1/projects", json={"name": "second try"}, headers=headers)
        assert second.status_code == 200
        assert second.json()["id"] == first_id
        # The duplicate name in the retried body was never persisted.
        assert second.json()["name"] == "first try"

        listing = client.get("/api/v1/projects").json()
        assert len(listing["items"]) == 1


class TestGet:
    def test_get_requires_membership(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, user_id=OWNER_ID)
        _login(app, user_id=OWNER_ID)

        response = client.get(f"/api/v1/projects/{project.id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(project.id)

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, user_id=OWNER_ID)
        _login(app, user_id=ANNOTATOR_ID)  # same org, no membership row

        response = client.get(f"/api/v1/projects/{project.id}")
        assert response.status_code == 403

    def test_404_for_another_organizations_project(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app, organization_id=ORG_ID)

        response = client.get(f"/api/v1/projects/{project.id}")
        assert response.status_code == 404


class TestUpdate:
    def test_patch_uses_exclude_unset_semantics(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, name="original", description="keep me")
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        # Omitted field ("description") is left untouched.
        renamed = client.patch(f"/api/v1/projects/{project.id}", json={"name": "renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "renamed"
        assert renamed.json()["description"] == "keep me"

        # Explicit null clears it.
        cleared = client.patch(f"/api/v1/projects/{project.id}", json={"description": None})
        assert cleared.status_code == 200
        assert cleared.json()["description"] is None
        assert cleared.json()["name"] == "renamed"

    def test_calibration_is_validated_and_the_rest_of_settings_is_free(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        path = f"/api/v1/projects/{project.id}"

        ok = client.patch(
            path,
            json={"settings": {"calibration": {"units_per_pixel": 0.05, "unit": "mm"}, "x": 1}},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["settings"] == {
            "calibration": {"units_per_pixel": 0.05, "unit": "mm"},
            "x": 1,
        }
        for bad in (
            {"units_per_pixel": 0, "unit": "mm"},
            {"units_per_pixel": -1, "unit": "mm"},
            {"units_per_pixel": 0.1, "unit": ""},
            {"units_per_pixel": 0.1, "unit": "a" * 17},
            {"units_per_pixel": 0.1},
            {"units_per_pixel": 0.1, "unit": "mm", "extra": True},
            "0.1 mm",
        ):
            response = client.patch(path, json={"settings": {"calibration": bad}})
            assert response.status_code == 422, bad
        cleared = client.patch(path, json={"settings": {"calibration": None}})
        assert cleared.status_code == 200
        assert cleared.json()["settings"] == {"calibration": None}

    def test_403_for_non_owner(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(
            sessionmaker, project_id=project.id, user_id=ANNOTATOR_ID, role=ProjectRole.ANNOTATOR
        )
        _login(app, user_id=ANNOTATOR_ID)

        response = client.patch(f"/api/v1/projects/{project.id}", json={"name": "nope"})
        assert response.status_code == 403


class TestCacheConnector:
    """SRC-6: moving `cache/` queues a rebuild; other edits never touch the queue."""

    @staticmethod
    def _install_queue(app: FastAPI, queue: FakeJobQueue) -> list[int]:
        calls: list[int] = []

        async def _get_queue() -> FakeJobQueue:
            calls.append(1)
            return queue

        app.dependency_overrides[get_job_queue] = _get_queue
        return calls

    def test_changing_the_effective_cache_connector_queues_a_rebuild(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        queue = FakeJobQueue()
        calls = self._install_queue(app, queue)
        path = f"/api/v1/projects/{project.id}"
        result = _seed_connector(sessionmaker)
        cache = _seed_connector(sessionmaker)

        # No cache connector yet: the result connector is where cache/ goes.
        response = client.patch(path, json={"result_connector_id": str(result)})
        assert response.status_code == 200, response.text
        first = uuid.UUID(response.headers["X-Rebuild-Job-Id"])
        assert queue.enqueued == [(first, "rebuild_cache")]

        response = client.patch(path, json={"cache_connector_id": str(cache)})
        assert response.status_code == 200
        assert response.json()["cache_connector_id"] == str(cache)
        second = uuid.UUID(response.headers["X-Rebuild-Job-Id"])
        assert queue.enqueued[-1] == (second, "rebuild_cache")

        # Same effective connector: a rename, or moving results while a
        # cache connector is set, neither queues nor even opens the queue.
        calls.clear()
        for body in ({"name": "renamed"}, {"result_connector_id": None}):
            response = client.patch(path, json=body)
            assert response.status_code == 200
            assert "X-Rebuild-Job-Id" not in response.headers
        assert calls == []
        assert len(queue.enqueued) == 2

    def test_an_unreachable_queue_does_not_fail_the_update(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        self._install_queue(app, FakeJobQueue(fail_with=QueueUnavailableError("down")))
        cache = _seed_connector(sessionmaker)

        response = client.patch(
            f"/api/v1/projects/{project.id}", json={"cache_connector_id": str(cache)}
        )
        assert response.status_code == 200, response.text
        assert response.json()["cache_connector_id"] == str(cache)
        assert "X-Rebuild-Job-Id" not in response.headers

    def test_http_cannot_be_the_cache_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _login(app)
        http = _seed_connector(sessionmaker, type_=ConnectorType.HTTP)
        response = client.post(
            "/api/v1/projects", json={"name": "p", "cache_connector_id": str(http)}
        )
        assert response.status_code == 422
        assert "cache connector" in response.text
        foreign = _seed_connector(sessionmaker, organization_id=OTHER_ORG_ID)
        response = client.post(
            "/api/v1/projects", json={"name": "p", "cache_connector_id": str(foreign)}
        )
        assert response.status_code == 404


class TestDelete:
    def test_delete_is_a_hard_delete(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.delete(f"/api/v1/projects/{project.id}")
        assert response.status_code == 204

        # Hard delete: the row is gone outright (no `deleted_at` column on
        # `Project`), so a superuser (bypassing membership) still gets a 404.
        _login(app, is_superuser=True)
        follow_up = client.get(f"/api/v1/projects/{project.id}")
        assert follow_up.status_code == 404

    def test_403_for_non_owner(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(
            sessionmaker, project_id=project.id, user_id=ANNOTATOR_ID, role=ProjectRole.VIEWER
        )
        _login(app, user_id=ANNOTATOR_ID)

        response = client.delete(f"/api/v1/projects/{project.id}")
        assert response.status_code == 403


class TestSchemaVersions:
    def test_versions_are_immutable_and_increment(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        first = client.post(f"/api/v1/projects/{project.id}/schemas", json=_VALID_SCHEMA_BODY)
        assert first.status_code == 201
        assert first.json()["version"] == 1

        second = client.post(f"/api/v1/projects/{project.id}/schemas", json=_VALID_SCHEMA_BODY)
        assert second.status_code == 201
        assert second.json()["version"] == 2
        # A new row, not an update of the first.
        assert second.json()["id"] != first.json()["id"]

        listing = client.get(f"/api/v1/projects/{project.id}/schemas")
        assert response_versions(listing) == [2, 1]  # newest first

    def test_403_for_non_owner(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(
            sessionmaker, project_id=project.id, user_id=ANNOTATOR_ID, role=ProjectRole.ANNOTATOR
        )
        _login(app, user_id=ANNOTATOR_ID)

        response = client.post(f"/api/v1/projects/{project.id}/schemas", json=_VALID_SCHEMA_BODY)
        assert response.status_code == 403


def response_versions(response: Any) -> list[int]:
    assert response.status_code == 200
    return [row["version"] for row in response.json()]


class TestPdfMode:
    """`settings.pdf_mode` (PDF text mode): values, and `text` needs a result connector."""

    def test_create_validates_the_value_and_the_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _login(app)
        connector = _seed_connector(sessionmaker)
        ok = client.post(
            "/api/v1/projects",
            json={
                "name": "t",
                "result_connector_id": str(connector),
                "settings": {"pdf_mode": "text"},
            },
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["settings"] == {"pdf_mode": "text"}
        layout = client.post(
            "/api/v1/projects", json={"name": "l", "settings": {"pdf_mode": "layout"}}
        )
        assert layout.status_code == 201
        null = client.post("/api/v1/projects", json={"name": "n", "settings": {"pdf_mode": None}})
        assert null.status_code == 201
        for bad in ("html", 1, True):
            response = client.post(
                "/api/v1/projects",
                json={
                    "name": "b",
                    "result_connector_id": str(connector),
                    "settings": {"pdf_mode": bad},
                },
            )
            assert response.status_code == 422, bad
        no_result = client.post(
            "/api/v1/projects", json={"name": "x", "settings": {"pdf_mode": "text"}}
        )
        assert no_result.status_code == 422

    def test_patch_rules(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        TestCacheConnector._install_queue(app, FakeJobQueue())
        path = f"/api/v1/projects/{project.id}"
        connector = _seed_connector(sessionmaker)

        assert client.patch(path, json={"settings": {"pdf_mode": "text"}}).status_code == 422
        both = client.patch(
            path, json={"result_connector_id": str(connector), "settings": {"pdf_mode": "text"}}
        )
        assert both.status_code == 200, both.text
        removed = client.patch(path, json={"result_connector_id": None})
        assert removed.status_code == 422
        # Switching back to layout (or null) first allows removing it.
        assert client.patch(path, json={"settings": {"pdf_mode": None}}).status_code == 200
        assert client.patch(path, json={"result_connector_id": None}).status_code == 200
