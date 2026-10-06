"""Tests for `api/v1/webhooks.py` (API-4, ML-9)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import httpx
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
    AuditEvent,
    Membership,
    Organization,
    Project,
    ProjectRole,
    Snapshot,
    Webhook,
    WebhookDelivery,
)
from app.services.webhooks import signing_secret

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, AuditEvent.__table__),
    cast(Table, Snapshot.__table__),
    cast(Table, Webhook.__table__),
    cast(Table, WebhookDelivery.__table__),
]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
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


def _user(org_id: UUID, *, is_superuser: bool = False) -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=org_id,
        email="p@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )


def _sign_in(app: FastAPI, user: CurrentUser) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        session.add(org)
        await session.flush()
        project = Project(organization_id=org.id, name="p", settings={}, workflow={})
        session.add(project)
        await session.commit()
        return org.id, project.id


async def _member(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: UUID, user: CurrentUser, role: str
) -> None:
    async with sessionmaker() as session:
        session.add(Membership(user_id=user.id, project_id=project_id, role=ProjectRole(role)))
        await session.commit()


def _create(client: TestClient, **body: object) -> httpx.Response:
    return client.post(
        "/api/v1/webhooks",
        json={"url": "https://hooks.example/in", "events": ["annotation.submitted"], **body},
    )


class TestCreate:
    async def test_superuser_creates_org_wide_hook_and_sees_secret_once(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))

        response = client.post(
            "/api/v1/webhooks",
            json={"url": "https://hooks.example/in", "events": ["*"], "description": "all"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["project_id"] is None
        assert body["events"] == ["*"]
        assert len(body["secret"]) == 64

        fetched = client.get(f"/api/v1/webhooks/{body['id']}")
        assert fetched.status_code == 200
        assert "secret" not in fetched.json()

        # At rest the column holds the sealed value, never the clear secret.
        async with sessionmaker() as session:
            stored = await session.get(Webhook, UUID(body["id"]))
        assert stored is not None
        assert stored.secret != body["secret"]
        assert signing_secret(stored) == body["secret"]

    async def test_a_chat_format_is_stored_and_can_be_changed(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))

        created = _create(client, format="slack")
        assert created.status_code == 201
        assert created.json()["format"] == "slack"
        assert _create(client).json()["format"] == "json"
        assert _create(client, format="email").status_code == 422

        hook_id = created.json()["id"]
        patched = client.patch(f"/api/v1/webhooks/{hook_id}", json={"format": "teams"})
        assert patched.status_code == 200
        assert client.get(f"/api/v1/webhooks/{hook_id}").json()["format"] == "teams"

    async def test_owner_creates_project_hook_but_not_org_wide(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)

        assert _create(client).status_code == 403
        response = _create(client, project_id=str(project_id))
        assert response.status_code == 201
        assert response.json()["project_id"] == str(project_id)

    async def test_annotator_may_not_create_project_hook(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        annotator = _user(org_id)
        await _member(sessionmaker, project_id, annotator, "annotator")
        _sign_in(app, annotator)

        response = _create(client, project_id=str(project_id))
        assert response.status_code == 403

    async def test_unknown_event_and_bad_url_are_422(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))

        assert _create(client, events=["item.exploded"]).status_code == 422
        assert _create(client, url="not a url").status_code == 422


class TestUpdateAndDelete:
    async def test_patch_changes_given_fields_and_rotates_secret_on_request(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        created = _create(client).json()

        response = client.patch(
            f"/api/v1/webhooks/{created['id']}", json={"is_active": False, "events": ["job.failed"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is False
        assert body["events"] == ["job.failed"]
        assert body["url"] == "https://hooks.example/in"
        assert body["secret"] is None

        rotated = client.patch(f"/api/v1/webhooks/{created['id']}", json={"rotate_secret": True})
        assert rotated.status_code == 200
        assert rotated.json()["secret"] not in (None, created["secret"])

    async def test_delete_and_cross_org_404(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        created = _create(client).json()

        _sign_in(app, _user(uuid4(), is_superuser=True))
        assert client.get(f"/api/v1/webhooks/{created['id']}").status_code == 404

        _sign_in(app, _user(org_id, is_superuser=True))
        assert client.delete(f"/api/v1/webhooks/{created['id']}").status_code == 204
        assert client.get(f"/api/v1/webhooks/{created['id']}").status_code == 404


class TestListTestAndDeliveries:
    async def test_list_is_scoped_and_test_queues_a_delivery(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        admin = _user(org_id, is_superuser=True)
        _sign_in(app, admin)
        org_hook = _create(client, events=["*"]).json()
        project_hook = _create(client, project_id=str(project_id)).json()

        org_list = client.get("/api/v1/webhooks").json()["items"]
        assert [h["id"] for h in org_list] == [org_hook["id"]]
        both = client.get("/api/v1/webhooks", params={"project_id": str(project_id)}).json()
        assert {h["id"] for h in both["items"]} == {org_hook["id"], project_hook["id"]}

        queued = client.post(f"/api/v1/webhooks/{project_hook['id']}/test")
        assert queued.status_code == 202
        assert queued.json()["event"] == "webhook.test"
        assert queued.json()["status"] == "pending"

        log = client.get(f"/api/v1/webhooks/{project_hook['id']}/deliveries")
        assert log.status_code == 200
        assert [d["event"] for d in log.json()["items"]] == ["webhook.test"]
        assert (
            client.get(
                f"/api/v1/webhooks/{project_hook['id']}/deliveries", params={"status": "succeeded"}
            ).json()["items"]
            == []
        )

    async def test_owner_sees_only_project_hooks(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        org_hook = _create(client, events=["*"]).json()
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        mine = _create(client, project_id=str(project_id)).json()

        assert client.get("/api/v1/webhooks").status_code == 403
        listed = client.get("/api/v1/webhooks", params={"project_id": str(project_id)}).json()
        assert [h["id"] for h in listed["items"]] == [mine["id"]]
        assert client.get(f"/api/v1/webhooks/{org_hook['id']}").status_code == 403


class TestRetrain:
    async def test_owner_emits_retrain_requested_with_snapshot_context(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        _create(client, project_id=str(project_id), events=["retrain.requested"])
        _create(client, project_id=str(project_id), events=["job.failed"])  # not subscribed
        async with sessionmaker() as session:
            snapshot = Snapshot(
                project_id=project_id,
                name="v3",
                filter={},
                label_schema_version_id=uuid4(),
                item_count=42,
                blob_path="snapshots/x/",
                digest="ab" * 32,
                created_by_id=owner.id,
            )
            session.add(snapshot)
            await session.commit()
            snapshot_id = snapshot.id

        response = client.post(
            f"/api/v1/projects/{project_id}/retrain",
            json={"snapshot_id": str(snapshot_id), "note": "weekly"},
        )

        assert response.status_code == 202
        assert response.json() == {"event": "retrain.requested", "deliveries": 1, "ml_run": None}
        async with sessionmaker() as session:
            [delivery] = list(await session.scalars(select(WebhookDelivery)))
        assert delivery.event == "retrain.requested"
        data = delivery.payload["data"]
        assert isinstance(data, dict)
        assert data["note"] == "weekly"
        assert data["snapshot"]["digest"] == "ab" * 32
        assert data["snapshot"]["item_count"] == 42

    async def test_retrain_is_owner_only_and_checks_the_snapshot(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_id = await _seed(sessionmaker)
        reviewer = _user(org_id)
        await _member(sessionmaker, project_id, reviewer, "reviewer")
        _sign_in(app, reviewer)
        assert client.post(f"/api/v1/projects/{project_id}/retrain", json={}).status_code == 403

        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        missing = client.post(
            f"/api/v1/projects/{project_id}/retrain", json={"snapshot_id": str(uuid4())}
        )
        assert missing.status_code == 404
        # No subscribers is fine: zero deliveries, still accepted.
        assert client.post(f"/api/v1/projects/{project_id}/retrain", json={}).json() == {
            "event": "retrain.requested",
            "deliveries": 0,
            "ml_run": None,
        }
