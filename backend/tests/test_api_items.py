"""Tests for `api/v1/items.py`.

No live database: each test gets its own in-memory SQLite database (a single
connection kept alive for the engine's lifetime via `StaticPool`, so every
session checkout during the test sees the same data), wired in through
`app.dependency_overrides[get_session]`. `app.dependency_overrides
[get_current_user]` stands in for a real bearer token. The `user` table is
deliberately not created — it uses a PostgreSQL-only `CITEXT` column that
SQLite cannot compile — which is fine here because nothing under test reads
`user` rows; the caller's identity comes entirely from the `CurrentUser`
override.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
    ItemStatus,
    MediaType,
    Membership,
    Model,
    ModelTask,
    Organization,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)
from app.services.models import ModelRejected, ModelUnavailable, PredictItem

_TABLES: list[Table] = [
    cast(Table, AuditEvent.__table__),
    cast(Table, Organization.__table__),
    cast(Table, Connector.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, Item.__table__),
    cast(Table, Task.__table__),
    cast(Table, Model.__table__),
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
    """Remove the auth override so a request follows the real, token-based path."""
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


async def _seed_project(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    connector_type: ConnectorType = ConnectorType.LOCAL,
    connector_config: dict[str, Any] | None = None,
    result_connector: bool = False,
) -> tuple[UUID, UUID, UUID]:
    """Insert an organization, a connector and a project. Returns their ids.

    With `result_connector`, the same connector is also the project's result
    connector, which is where thumbnails are signed from (IMG-8).
    """
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()

        connector = Connector(
            organization_id=org.id,
            name="source",
            type=connector_type,
            identity_type=ConnectorIdentity.NONE,
            config=connector_config if connector_config is not None else {"root": "/tmp/fixture"},
        )
        session.add(connector)
        await session.flush()

        project = Project(
            organization_id=org.id,
            name="Project 1",
            result_connector_id=connector.id if result_connector else None,
        )
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
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    connector_id: UUID,
    path: str = "images/a.jpg",
    status: ItemStatus = ItemStatus.NEW,
    thumbnail_path: str | None = None,
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
            thumbnail_path=thumbnail_path,
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
    assignee_id: UUID | None,
) -> None:
    async with sessionmaker() as session:
        session.add(
            Task(
                project_id=project_id,
                item_id=item_id,
                type=TaskType.ANNOTATE,
                assignee_id=assignee_id,
            )
        )
        await session.commit()


class TestListItems:
    async def test_requires_authentication(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, _, project_id = await _seed_project(sessionmaker)
        _sign_out(app)

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 401

    async def test_non_member_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, project_id = await _seed_project(sessionmaker)
        # Same organisation, but never added as a project member.
        _sign_in(app, _make_user(org_id))

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 403

    async def test_cross_organisation_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, _, project_id = await _seed_project(sessionmaker)
        other_org_id = uuid4()
        _sign_in(app, _make_user(other_org_id))

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 404

    async def test_lists_items_for_a_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, user)

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["path"] == "images/a.jpg"
        # The grid previews from the list, so every row is signed too (UX-6).
        assert body["items"][0]["media_url"] is not None
        assert "images/a.jpg" in body["items"][0]["media_url"]

    async def test_signs_thumbnails_through_the_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker, result_connector=True)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        with_thumb = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/a.jpg",
            thumbnail_path="cache/thumbnails/a.jpg",
        )
        without = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="images/b.jpg"
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["items"]}
        assert "cache/thumbnails/a.jpg" in rows[str(with_thumb)]["thumbnail_url"]
        assert rows[str(without)]["thumbnail_url"] is None

    async def test_thumbnail_url_is_none_without_a_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            thumbnail_path="cache/thumbnails/a.jpg",
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/projects/{project_id}/items")

        assert response.status_code == 200
        assert response.json()["items"][0]["thumbnail_url"] is None

    async def test_filters_by_status(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="a.jpg",
            status=ItemStatus.NEW,
        )
        await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="b.jpg",
            status=ItemStatus.APPROVED,
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/projects/{project_id}/items", params={"status": "approved"})

        assert response.status_code == 200
        body = response.json()
        assert [item["path"] for item in body["items"]] == ["b.jpg"]

    async def test_filters_by_media_type(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        async with sessionmaker() as session:
            session.add(
                Item(
                    project_id=project_id,
                    connector_id=connector_id,
                    path="clip.mp4",
                    media_type=MediaType.VIDEO,
                    size_bytes=1,
                    meta={},
                )
            )
            await session.commit()
        await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="a.jpg"
        )
        _sign_in(app, user)

        response = client.get(
            f"/api/v1/projects/{project_id}/items", params={"media_type": "video"}
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["path"] for item in body["items"]] == ["clip.mp4"]

    async def test_filters_by_path_substring(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="cats/1.jpg"
        )
        await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="dogs/1.jpg"
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/projects/{project_id}/items", params={"q": "cats"})

        assert response.status_code == 200
        body = response.json()
        assert [item["path"] for item in body["items"]] == ["cats/1.jpg"]

    async def test_filters_by_assignee(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        mine = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="mine.jpg"
        )
        await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="theirs.jpg"
        )
        assignee = uuid4()
        await _add_task(sessionmaker, project_id=project_id, item_id=mine, assignee_id=assignee)
        _sign_in(app, user)

        response = client.get(
            f"/api/v1/projects/{project_id}/items", params={"assignee_id": str(assignee)}
        )

        assert response.status_code == 200
        body = response.json()
        assert [item["path"] for item in body["items"]] == ["mine.jpg"]


class TestCreateItem:
    async def test_creates_a_new_item(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.OWNER)
        _sign_in(app, user)

        response = client.post(
            f"/api/v1/projects/{project_id}/items",
            json={
                "connector_id": str(connector_id),
                "path": "images/new.jpg",
                "media_type": "image",
                "size_bytes": 42,
            },
        )

        assert response.status_code == 201
        assert response.json()["path"] == "images/new.jpg"

    async def test_a_client_cannot_set_pdf_text(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`meta.pdf_text` names the object signed as the item's text; only the platform sets it."""
        org_id, connector_id, project_id = await _seed_project(sessionmaker, result_connector=True)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.OWNER)
        _sign_in(app, user)

        response = client.post(
            f"/api/v1/projects/{project_id}/items",
            json={
                "connector_id": str(connector_id),
                "path": "docs/a.pdf",
                "media_type": "text",
                "size_bytes": 42,
                "meta": {"pdf_text": {"status": "ready", "path": "exports/other.zip"}},
            },
        )

        assert response.status_code == 422
        assert "pdf_text" in response.text

    async def test_duplicate_is_idempotent_and_returns_200(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.OWNER)
        _sign_in(app, user)
        payload = {
            "connector_id": str(connector_id),
            "path": "images/dup.jpg",
            "media_type": "image",
            "size_bytes": 42,
        }

        first = client.post(f"/api/v1/projects/{project_id}/items", json=payload)
        second = client.post(f"/api/v1/projects/{project_id}/items", json=payload)

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]


class TestGetItem:
    async def test_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed_project(sessionmaker)
        _sign_in(app, _make_user(org_id))

        response = client.get(f"/api/v1/items/{uuid4()}")

        assert response.status_code == 404

    async def test_cross_organisation_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id = await _seed_project(sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, _make_user(uuid4()))

        response = client.get(f"/api/v1/items/{item_id}")

        assert response.status_code == 404

    async def test_returns_a_signed_media_url(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, user)

        response = client.get(f"/api/v1/items/{item_id}")

        assert response.status_code == 200
        media_url = response.json()["media_url"]
        assert media_url is not None
        assert "images/a.jpg" in media_url

    async def test_returns_a_signed_thumbnail_url_when_one_exists(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker, result_connector=True)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            thumbnail_path="cache/thumbnails/a.jpg",
        )
        _sign_in(app, user)

        response = client.get(f"/api/v1/items/{item_id}")

        assert response.status_code == 200
        assert "cache/thumbnails/a.jpg" in response.json()["thumbnail_url"]

    async def test_signed_url_ttl_is_passed_through(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, user)
        custom_ttl = 123
        app.dependency_overrides[get_settings] = lambda: Settings(
            database_url="sqlite+aiosqlite:///:memory:", signed_url_ttl=custom_ttl
        )

        before = int(time.time())
        response = client.get(f"/api/v1/items/{item_id}")
        after = int(time.time())

        assert response.status_code == 200
        media_url = response.json()["media_url"]
        assert media_url is not None
        expires = int(parse_qs(urlparse(media_url).query)["expires"][0])
        assert before + custom_ttl <= expires <= after + custom_ttl

    async def test_connector_that_cannot_sign_yields_none_not_500(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(
            sessionmaker, connector_type=ConnectorType.S3, connector_config={}
        )
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        _sign_in(app, user)

        response = client.get(f"/api/v1/items/{item_id}")

        assert response.status_code == 200
        assert response.json()["media_url"] is None


async def _add_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    *,
    task: ModelTask = ModelTask.SEGMENT,
) -> UUID:
    async with sessionmaker() as session:
        model = Model(
            organization_id=organization_id,
            name="segmenter",
            task=task,
            endpoint_url="http://model.example.com",
            identity_type="none",
            secret_ref=None,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return model.id


class _StubModelClient:
    """Stands in for `ModelClient`: records the prompt, answers a fixed polygon."""

    calls: ClassVar[list[dict[str, Any]]] = []
    answer: ClassVar[Any] = None

    async def __aenter__(self) -> _StubModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def interactive(self, item: PredictItem, **prompt: Any) -> Any:
        type(self).calls.append({"item": item, **prompt})
        if isinstance(type(self).answer, Exception):
            raise type(self).answer
        return type(self).answer

    async def ocr(self, item: PredictItem, page: int) -> Any:
        type(self).calls.append({"item": item, "page": page})
        if isinstance(type(self).answer, Exception):
            raise type(self).answer
        return type(self).answer


@pytest.fixture
def stub_model_client(monkeypatch: pytest.MonkeyPatch) -> type[_StubModelClient]:
    class _Stub(_StubModelClient):
        calls: ClassVar[list[dict[str, Any]]] = []
        answer: ClassVar[Any] = None

    stub = _Stub

    async def _for_model(*args: Any, **kwargs: Any) -> _StubModelClient:
        return stub()

    monkeypatch.setattr("app.api.v1.items.ModelClient.for_model", _for_model)
    return stub


class TestInteractive:
    """`POST /items/{id}/interactive` (ML-7)."""

    async def _seed(
        self, app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> tuple[UUID, UUID, UUID]:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            item.width, item.height = 640, 480
            await session.commit()
        _sign_in(app, user)
        return org_id, project_id, item_id

    async def test_point_prompt_returns_polygon(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        from app.services.models import InteractivePolygon

        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id)
        stub_model_client.answer = InteractivePolygon(
            points=[(10.0, 10.0), (50.0, 10.0), (50.0, 40.0)], confidence=0.8
        )

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "point": {"x": 30, "y": 20}},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "type": "polygon",
            "points": [[10.0, 10.0], [50.0, 10.0], [50.0, 40.0]],
            "confidence": 0.8,
        }
        call = stub_model_client.calls[0]
        assert call["point"] == (30.0, 20.0)
        assert call["box"] is None
        assert call["item"].width == 640 and call["item"].height == 480
        assert "images/a.jpg" in call["item"].url

    async def test_box_prompt_is_forwarded(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        from app.services.models import InteractivePolygon

        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id)
        stub_model_client.answer = InteractivePolygon(
            points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)], confidence=0.5
        )

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "box": [10, 20, 110, 220]},
        )

        assert response.status_code == 200, response.text
        assert stub_model_client.calls[0]["box"] == (10.0, 20.0, 110.0, 220.0)
        assert stub_model_client.calls[0]["point"] is None

    @pytest.mark.parametrize(
        "prompt",
        [
            {},
            {"point": {"x": 1, "y": 1}, "box": [0, 0, 5, 5]},
            {"box": [10, 10, 5, 20]},
        ],
    )
    async def test_exactly_one_valid_prompt(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
        prompt: dict[str, Any],
    ) -> None:
        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id)

        response = client.post(
            f"/api/v1/items/{item_id}/interactive", json={"model_id": str(model_id), **prompt}
        )

        assert response.status_code == 422
        assert stub_model_client.calls == []

    async def test_non_segment_model_is_a_conflict(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id, task=ModelTask.DETECT)

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "point": {"x": 1, "y": 1}},
        )

        assert response.status_code == 409
        assert stub_model_client.calls == []

    async def test_model_of_another_organisation_is_not_found(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        _, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, uuid4())

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "point": {"x": 1, "y": 1}},
        )

        assert response.status_code == 404

    async def test_non_member_is_forbidden(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id)
        _sign_in(app, _make_user(org_id))

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "point": {"x": 1, "y": 1}},
        )

        assert response.status_code == 403

    @pytest.mark.parametrize("error", [ModelUnavailable("down"), ModelRejected("HTTP 400 nope")])
    async def test_model_failure_is_503(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
        error: Exception,
    ) -> None:
        org_id, _, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id)
        stub_model_client.answer = error

        response = client.post(
            f"/api/v1/items/{item_id}/interactive",
            json={"model_id": str(model_id), "point": {"x": 1, "y": 1}},
        )

        assert response.status_code == 503
        body = response.json()
        assert body["type"].endswith("model-unavailable")
        assert str(error) in body["detail"]


class TestOcr:
    """`POST /items/{id}/ocr`: the words of a scanned page, through an ocr model."""

    async def _seed(
        self,
        app: FastAPI,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        media_type: MediaType = MediaType.PDF,
    ) -> tuple[UUID, UUID]:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="docs/scan.pdf"
        )
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            item.media_type = media_type
            item.meta = {"page_count": 2, "text_layer": False}
            await session.commit()
        _sign_in(app, user)
        return org_id, item_id

    async def test_returns_the_words_of_the_page(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        from app.services.models import OcrPage

        org_id, item_id = await self._seed(app, sessionmaker)
        model_id = await _add_model(sessionmaker, org_id, task=ModelTask.OCR)
        stub_model_client.answer = OcrPage(
            page=2,
            width=600.0,
            height=800.0,
            engine="anthropic",
            words=[("Total", (100.0, 90.0, 140.0, 100.0))],
        )

        response = client.post(
            f"/api/v1/items/{item_id}/ocr", json={"model_id": str(model_id), "page": 2}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "page": 2,
            "width": 600.0,
            "height": 800.0,
            "engine": "anthropic",
            "words": [{"text": "Total", "bbox": [100.0, 90.0, 140.0, 100.0]}],
        }
        [call] = stub_model_client.calls
        assert call["page"] == 2
        assert call["item"].media_type == "pdf"
        assert "docs/scan.pdf" in call["item"].url

    async def test_refuses_other_models_items_and_pages(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        org_id, item_id = await self._seed(app, sessionmaker)
        segmenter = await _add_model(sessionmaker, org_id)
        reader = await _add_model(sessionmaker, org_id, task=ModelTask.OCR)
        url = f"/api/v1/items/{item_id}/ocr"

        wrong_task = client.post(url, json={"model_id": str(segmenter), "page": 1})
        assert wrong_task.status_code == 409
        assert "not an ocr model" in wrong_task.json()["detail"]
        past_the_end = client.post(url, json={"model_id": str(reader), "page": 3})
        assert past_the_end.status_code == 422
        assert client.post(url, json={"model_id": str(reader), "page": 0}).status_code == 422
        unknown = client.post(url, json={"model_id": str(uuid4()), "page": 1})
        assert unknown.status_code == 404
        assert stub_model_client.calls == []

    async def test_an_image_item_is_a_conflict(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        org_id, item_id = await self._seed(app, sessionmaker, media_type=MediaType.IMAGE)
        reader = await _add_model(sessionmaker, org_id, task=ModelTask.OCR)
        response = client.post(
            f"/api/v1/items/{item_id}/ocr", json={"model_id": str(reader), "page": 1}
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "OCR reads pdf items."

    async def test_a_failing_model_is_503(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        stub_model_client: type[_StubModelClient],
    ) -> None:
        from app.services.models import ModelUnavailable

        org_id, item_id = await self._seed(app, sessionmaker)
        reader = await _add_model(sessionmaker, org_id, task=ModelTask.OCR)
        stub_model_client.answer = ModelUnavailable("POST /ocr: HTTP 502")
        response = client.post(
            f"/api/v1/items/{item_id}/ocr", json={"model_id": str(reader), "page": 1}
        )
        assert response.status_code == 503


class TestSkipItem:
    async def test_skip_requires_a_reason(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/items/{item_id}/skip", json={})

        assert response.status_code == 422

    async def test_skip_moves_item_and_records_reason(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/items/{item_id}/skip", json={"reason": "blurry image"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "skipped"
        assert body["meta"]["skip_reason"] == "blurry image"

    async def test_skip_illegal_for_role_is_a_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        # A viewer cannot fire the SKIP trigger at all (app.services.workflow).
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.VIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/items/{item_id}/skip", json={"reason": "n/a"})

        assert response.status_code == 409

    async def test_skip_refused_when_the_project_disallows_it(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`allow_skip: false` removes the trigger for everyone (WF-1)."""
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        async with sessionmaker() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            project.workflow = {"allow_skip": False}
            await session.commit()
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.OWNER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        _sign_in(app, user)

        response = client.post(f"/api/v1/items/{item_id}/skip", json={"reason": "blurry"})

        assert response.status_code == 409
        assert client.get(f"/api/v1/items/{item_id}").json()["status"] == "annotating"

    async def test_skip_completes_the_annotate_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Leaving the queue via skip also closes out its annotate task (WF-2)."""
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.ANNOTATING,
        )
        async with sessionmaker() as session:
            task = Task(
                project_id=project_id,
                item_id=item_id,
                type=TaskType.ANNOTATE,
                status=TaskStatus.IN_PROGRESS,
                assignee_id=user.id,
                locked_by_id=user.id,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id
        _sign_in(app, user)

        response = client.post(f"/api/v1/items/{item_id}/skip", json={"reason": "blurry image"})

        assert response.status_code == 200
        async with sessionmaker() as session:
            stored = await session.get(Task, task_id)
            assert stored is not None
            assert stored.status is TaskStatus.DONE
            assert stored.locked_by_id is None
            assert stored.locked_until is None


class TestPdfTextModeMediaUrl:
    """A PDF taken in as text is signed at its extracted text (CONTRACTS.md *PDF text mode*)."""

    async def _add_pdf_text_item(
        self, sessionmaker: async_sessionmaker[AsyncSession], project_id: UUID, connector_id: UUID
    ) -> UUID:
        async with sessionmaker() as session:
            item = Item(
                project_id=project_id,
                connector_id=connector_id,
                path="docs/contract.pdf",
                media_type=MediaType.TEXT,
                size_bytes=100,
                meta={"pdf_text": {"status": "pending"}},
                status=ItemStatus.NEW,
            )
            session.add(item)
            await session.commit()
            return item.id

    async def _mark_ready(
        self, sessionmaker: async_sessionmaker[AsyncSession], item_id: UUID
    ) -> None:
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            item.meta = {"pdf_text": {"status": "ready", "path": f"text/{item_id}.txt"}}
            await session.commit()

    async def test_no_media_url_until_the_text_is_ready_then_the_text_file(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker, result_connector=True)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await self._add_pdf_text_item(sessionmaker, project_id, connector_id)
        _sign_in(app, user)

        assert client.get(f"/api/v1/items/{item_id}").json()["media_url"] is None
        listed = client.get(f"/api/v1/projects/{project_id}/items").json()["items"]
        assert listed[0]["media_url"] is None

        await self._mark_ready(sessionmaker, item_id)

        media_url = client.get(f"/api/v1/items/{item_id}").json()["media_url"]
        assert media_url is not None
        assert f"text/{item_id}.txt" in media_url
        assert "contract.pdf" not in media_url
        listed = client.get(f"/api/v1/projects/{project_id}/items").json()["items"]
        assert f"text/{item_id}.txt" in listed[0]["media_url"]

    async def test_no_media_url_without_a_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, connector_id, project_id = await _seed_project(sessionmaker)
        user = _make_user(org_id)
        await _add_member(sessionmaker, project_id, user.id, ProjectRole.ANNOTATOR)
        item_id = await self._add_pdf_text_item(sessionmaker, project_id, connector_id)
        await self._mark_ready(sessionmaker, item_id)
        _sign_in(app, user)

        assert client.get(f"/api/v1/items/{item_id}").json()["media_url"] is None
