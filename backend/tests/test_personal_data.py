"""Tests for `api/v1/users.py` and `services/personal_data.py` (SEC-6).

Same real-SQLite setup as `tests/test_api_comments.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
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
    ApiKey,
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
    NotificationType,
    Organization,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.services.personal_data import ERASED_DISPLAY_NAME, erased_email


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, model.__table__)
    for model in (
        Organization,
        User,
        Connector,
        Project,
        Membership,
        Item,
        Task,
        LabelSchema,
        LabelSchemaVersion,
        Annotation,
        Comment,
        Notification,
        ApiKey,
        AuditEvent,
    )
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


def _sign_in(
    app: FastAPI, user_id: UUID, org_id: UUID, *, superuser: bool = False, service: bool = False
) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=user_id,
        organization_id=org_id,
        email="caller@example.com",
        is_superuser=superuser,
        is_service=service,
        scopes=frozenset({"admin"}) if service else frozenset(),
    )


@dataclass
class World:
    org_id: UUID
    admin_id: UUID
    person_id: UUID
    other_id: UUID
    project_id: UUID
    live_task_id: UUID
    done_task_id: UUID
    comment_id: UUID
    annotation_id: UUID


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        admin = User(
            organization_id=org.id,
            email="admin@example.com",
            display_name="Admin",
            is_active=True,
            is_superuser=True,
        )
        person = User(
            organization_id=org.id,
            email="anna@example.com",
            display_name="Anna Annotator",
            idp_subject="sub-anna",
            password_hash="argon2-hash",
            totp_secret="sealed",
            totp_enabled_at=datetime.now(UTC),
            mfa_recovery_codes=["abc"],
            last_seen_at=datetime.now(UTC),
            is_active=True,
            is_superuser=True,
        )
        other = User(
            organization_id=org.id,
            email="bob@example.com",
            display_name="Bob",
            is_active=True,
            is_superuser=False,
        )
        session.add_all([admin, person, other])
        connector = Connector(
            organization_id=org.id,
            name="local",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp"},
        )
        session.add(connector)
        await session.flush()
        project = Project(
            organization_id=org.id, name="Roads", source_connector_id=connector.id, workflow={}
        )
        session.add(project)
        await session.flush()
        schema = LabelSchema(project_id=project.id, name="s")
        session.add(schema)
        await session.flush()
        version = LabelSchemaVersion(
            label_schema_id=schema.id, version=1, definition={"version": 1, "classes": []}
        )
        session.add(version)
        session.add(Membership(user_id=person.id, project_id=project.id, role=ProjectRole.OWNER))
        item = Item(
            project_id=project.id,
            connector_id=connector.id,
            path="a.png",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            meta={},
            status=ItemStatus.NEW,
        )
        session.add(item)
        await session.flush()
        live = Task(
            item_id=item.id,
            project_id=project.id,
            type=TaskType.ANNOTATE,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=person.id,
            locked_by_id=person.id,
            locked_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        done = Task(
            item_id=item.id,
            project_id=project.id,
            type=TaskType.REVIEW,
            status=TaskStatus.DONE,
            assignee_id=person.id,
        )
        session.add_all([live, done])
        annotation = Annotation(
            item_id=item.id,
            version=1,
            author_user_id=person.id,
            source="human",
            label_schema_version_id=version.id,
            result={"schema_version": 1, "media_type": "image", "shapes": []},
        )
        session.add(annotation)
        await session.flush()
        comment = Comment(
            project_id=project.id, item_id=item.id, author_id=person.id, body="Looks off, @bob"
        )
        session.add(comment)
        session.add(
            Notification(user_id=person.id, type=NotificationType.MENTION, payload={"x": 1})
        )
        session.add(
            ApiKey(organization_id=org.id, user_id=person.id, name="laptop", scopes=["read"])
        )
        session.add_all(
            [
                AuditEvent(
                    organization_id=org.id,
                    actor_id=person.id,
                    action="auth.login",
                    target_type="user",
                    target_id=person.id,
                    ip="10.0.0.7",
                    after={"email": "anna@example.com", "method": "oidc"},
                ),
                AuditEvent(
                    organization_id=org.id,
                    actor_id=other.id,
                    action="auth.login",
                    target_type="user",
                    target_id=other.id,
                    ip="10.0.0.8",
                    after={"email": "bob@example.com"},
                ),
            ]
        )
        await session.commit()
        return World(
            org_id=org.id,
            admin_id=admin.id,
            person_id=person.id,
            other_id=other.id,
            project_id=project.id,
            live_task_id=live.id,
            done_task_id=done.id,
            comment_id=comment.id,
            annotation_id=annotation.id,
        )


# --- access ------------------------------------------------------------------


async def test_person_exports_own_data(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.person_id, world.org_id)

    response = client.get(f"/api/v1/users/{world.person_id}/personal-data")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "anna@example.com"
    assert body["user"]["idp_linked"] is True
    assert body["user"]["mfa_enabled"] is True
    for secret in ("password_hash", "totp_secret", "mfa_recovery_codes", "idp_subject"):
        assert secret not in body["user"]
    assert body["memberships"] == [
        {
            "project_id": str(world.project_id),
            "project_name": "Roads",
            "role": "owner",
            "created_at": body["memberships"][0]["created_at"],
        }
    ]
    assert [c["body"] for c in body["comments"]] == ["Looks off, @bob"]
    assert [a["id"] for a in body["annotations"]] == [str(world.annotation_id)]
    assert "result" not in body["annotations"][0]
    assert {t["id"] for t in body["tasks"]} == {str(world.live_task_id), str(world.done_task_id)}
    assert len(body["notifications"]) == 1
    assert [k["name"] for k in body["api_keys"]] == ["laptop"]
    assert [e["ip"] for e in body["audit_events"]] == ["10.0.0.7"]

    async with sessionmaker() as session:
        actions = (await session.scalars(select(AuditEvent.action))).all()
    assert "user.export_personal_data" in actions


async def test_non_admin_cannot_export_someone_else(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.other_id, world.org_id)

    response = client.get(f"/api/v1/users/{world.person_id}/personal-data")

    assert response.status_code == 403


async def test_admin_exports_anyone_but_not_across_organisations(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.admin_id, world.org_id, superuser=True)
    assert client.get(f"/api/v1/users/{world.person_id}/personal-data").status_code == 200

    _sign_in(app, world.admin_id, uuid4(), superuser=True)
    assert client.get(f"/api/v1/users/{world.person_id}/personal-data").status_code == 404


async def test_api_keys_cannot_export(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.person_id, world.org_id, service=True)

    assert client.get(f"/api/v1/users/{world.person_id}/personal-data").status_code == 403


# --- erasure -----------------------------------------------------------------


def _erase(client: TestClient, user_id: UUID, **body: object) -> httpx.Response:
    payload = {"confirm_email": "anna@example.com", **body}
    response: httpx.Response = client.post(f"/api/v1/users/{user_id}/erase", json=payload)
    return response


async def test_erase_pseudonymises_and_scrubs(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.admin_id, world.org_id, superuser=True)

    response = client.post(
        f"/api/v1/users/{world.person_id}/erase", json={"confirm_email": " Anna@Example.com "}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == erased_email(world.person_id)
    assert body["display_name"] == ERASED_DISPLAY_NAME
    assert body["is_active"] is False
    assert body["is_superuser"] is False
    assert body["idp_subject"] is None
    assert body["mfa_enabled"] is False

    async with sessionmaker() as session:
        user = await session.get(User, world.person_id)
        assert user is not None
        assert user.password_hash is None
        assert user.totp_secret is None
        assert user.mfa_recovery_codes is None
        assert user.last_seen_at is None
        assert user.erased_at is not None
        assert (await session.scalars(select(Membership))).all() == []
        assert (await session.scalars(select(Notification))).all() == []
        key = (await session.scalars(select(ApiKey))).one()
        assert key.revoked_at is not None
        live = await session.get(Task, world.live_task_id)
        assert live is not None
        assert live.status == TaskStatus.OPEN
        assert live.assignee_id is None
        assert live.locked_by_id is None
        assert live.locked_until is None
        done = await session.get(Task, world.done_task_id)
        assert done is not None
        assert done.assignee_id == world.person_id
        comment = await session.get(Comment, world.comment_id)
        assert comment is not None
        assert comment.body == "Looks off, @bob"
        annotation = await session.get(Annotation, world.annotation_id)
        assert annotation is not None
        assert annotation.author_user_id == world.person_id

        events = (await session.scalars(select(AuditEvent))).all()
        by_action = {(e.action, e.actor_id): e for e in events}
        own_login = by_action[("auth.login", world.person_id)]
        assert own_login.ip is None
        assert own_login.after == {"method": "oidc"}
        other_login = by_action[("auth.login", world.other_id)]
        assert other_login.ip is not None
        assert other_login.after == {"email": "bob@example.com"}
        erase = by_action[("user.erase", world.admin_id)]
        assert erase.target_id == world.person_id
        assert erase.after == {"redact_comments": False}


async def test_erase_can_redact_comments(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    _sign_in(app, world.admin_id, world.org_id, superuser=True)

    assert _erase(client, world.person_id, redact_comments=True).status_code == 200

    async with sessionmaker() as session:
        comment = await session.get(Comment, world.comment_id)
        assert comment is not None
        assert comment.body == "[erased]"


async def test_erase_guards(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)

    _sign_in(app, world.other_id, world.org_id)
    assert _erase(client, world.person_id).status_code == 403

    _sign_in(app, world.admin_id, world.org_id, superuser=True, service=True)
    assert _erase(client, world.person_id).status_code == 403

    _sign_in(app, world.admin_id, world.org_id, superuser=True)
    wrong = client.post(
        f"/api/v1/users/{world.person_id}/erase", json={"confirm_email": "bob@example.com"}
    )
    assert wrong.status_code == 422
    self_erase = client.post(
        f"/api/v1/users/{world.admin_id}/erase", json={"confirm_email": "admin@example.com"}
    )
    assert self_erase.status_code == 409
    assert _erase(client, uuid4()).status_code == 404

    assert _erase(client, world.person_id).status_code == 200
    again = client.post(
        f"/api/v1/users/{world.person_id}/erase",
        json={"confirm_email": erased_email(world.person_id)},
    )
    assert again.status_code == 409


# --- directory -----------------------------------------------------------------


async def test_admin_lists_people_with_filter_and_erased_flag(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            User(
                organization_id=world.org_id,
                email=f"svc-{uuid4()}@service.invalid",
                display_name="CI bot",
                is_service=True,
            )
        )
        await session.commit()
    _sign_in(app, world.admin_id, world.org_id, superuser=True)
    assert _erase(client, world.person_id).status_code == 200

    everyone = client.get("/api/v1/users")
    assert everyone.status_code == 200
    rows = everyone.json()
    assert [row["email"] for row in rows] == sorted(row["email"] for row in rows)
    assert all(not row["is_service"] for row in rows)
    erased = next(row for row in rows if row["id"] == str(world.person_id))
    assert erased["erased_at"] is not None
    assert {str(world.admin_id), str(world.other_id)} <= {row["id"] for row in rows}

    by_name = client.get("/api/v1/users", params={"q": "BOB"}).json()
    assert [row["id"] for row in by_name] == [str(world.other_id)]
    assert client.get("/api/v1/users", params={"q": "%"}).json() == []
    assert len(client.get("/api/v1/users", params={"limit": 1}).json()) == 1
    assert client.get("/api/v1/users", params={"limit": 0}).status_code == 422


async def test_user_directory_is_superuser_only_and_per_organisation(
    app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _seed(sessionmaker)

    _sign_in(app, world.other_id, world.org_id)
    assert client.get("/api/v1/users").status_code == 403
    _sign_in(app, world.admin_id, world.org_id, superuser=True, service=True)
    assert client.get("/api/v1/users").status_code == 403
    _sign_in(app, world.admin_id, uuid4(), superuser=True)
    assert client.get("/api/v1/users").json() == []
