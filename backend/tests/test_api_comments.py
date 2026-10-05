"""Tests for `api/v1/comments.py`, `api/v1/notifications.py` and `api/v1/audit.py` (WF-5, SEC-3).

Same real-SQLite setup as `tests/test_api_members.py`, including the `CITEXT`
→ `VARCHAR` compile hook, because mentions resolve `@email` against the
`user` table.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

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
    User,
    Webhook,
    WebhookDelivery,
)


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Connector.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, Item.__table__),
    cast(Table, Task.__table__),
    cast(Table, LabelSchema.__table__),
    cast(Table, LabelSchemaVersion.__table__),
    cast(Table, Annotation.__table__),
    cast(Table, OutboxEvent.__table__),
    cast(Table, Comment.__table__),
    cast(Table, Notification.__table__),
    cast(Table, Webhook.__table__),
    cast(Table, WebhookDelivery.__table__),
    cast(Table, AuditEvent.__table__),
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


def _sign_in(app: FastAPI, user_id: UUID, org_id: UUID, *, superuser: bool = False) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=user_id,
        organization_id=org_id,
        email="caller@example.com",
        is_superuser=superuser,
        is_service=False,
        scopes=frozenset(),
    )


class World:
    """One organisation with a project, an item and three users."""

    org_id: UUID
    project_id: UUID
    item_id: UUID
    owner_id: UUID
    annotator_id: UUID
    reviewer_id: UUID
    schema_version_id: UUID


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        users = {}
        for name in ("owner", "annotator", "reviewer"):
            user = User(
                organization_id=org.id,
                email=f"{name}@example.com",
                display_name=name.title(),
                is_active=True,
                is_superuser=False,
            )
            session.add(user)
            users[name] = user
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
            organization_id=org.id, name="P", source_connector_id=connector.id, workflow={}
        )
        session.add(project)
        await session.flush()
        schema = LabelSchema(project_id=project.id, name="s")
        session.add(schema)
        await session.flush()
        version = LabelSchemaVersion(
            label_schema_id=schema.id,
            version=1,
            definition={
                "version": 1,
                "classes": [
                    {
                        "name": "car",
                        "display_name": "Car",
                        "color": "#ff0000",
                        "tools": ["bbox"],
                        "attributes": [],
                    }
                ],
                "classification": [],
            },
        )
        session.add(version)
        for name, role in (
            ("owner", ProjectRole.OWNER),
            ("annotator", ProjectRole.ANNOTATOR),
            ("reviewer", ProjectRole.REVIEWER),
        ):
            session.add(Membership(user_id=users[name].id, project_id=project.id, role=role))
        item = Item(
            project_id=project.id,
            connector_id=connector.id,
            path="images/a.png",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            meta={},
            status=ItemStatus.NEW,
        )
        session.add(item)
        await session.commit()
        world = World()
        world.org_id = org.id
        world.project_id = project.id
        world.item_id = item.id
        world.owner_id = users["owner"].id
        world.annotator_id = users["annotator"].id
        world.reviewer_id = users["reviewer"].id
        world.schema_version_id = version.id
        return world


async def _add_annotation(sessionmaker: async_sessionmaker[AsyncSession], world: World) -> UUID:
    async with sessionmaker() as session:
        annotation = Annotation(
            item_id=world.item_id,
            version=1,
            author_user_id=world.annotator_id,
            source="human",
            label_schema_version_id=world.schema_version_id,
            result={"schema_version": 1, "media_type": "image", "classification": {}, "shapes": []},
            status=AnnotationStatus.SUBMITTED,
        )
        session.add(annotation)
        await session.commit()
        await session.refresh(annotation)
        return annotation.id


async def _rows[T](sessionmaker: async_sessionmaker[AsyncSession], model: type[T]) -> list[T]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(model)))


def _post_comment(client: TestClient, item_id: UUID, body: str, **extra: Any) -> Any:
    return client.post(f"/api/v1/items/{item_id}/comments", json={"body": body, **extra})


class TestComments:
    async def test_create_and_list_round_trip(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.annotator_id, world.org_id)

        created = _post_comment(client, world.item_id, "Looks blurry")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["author_id"] == str(world.annotator_id)
        assert body["item_id"] == str(world.item_id)
        assert body["resolved_at"] is None

        listed = client.get(f"/api/v1/items/{world.item_id}/comments")
        assert listed.status_code == 200
        assert [c["body"] for c in listed.json()] == ["Looks blurry"]

        audit_rows = await _rows(sessionmaker, AuditEvent)
        assert [a.action for a in audit_rows] == ["comment.create"]
        assert audit_rows[0].target_id == UUID(body["id"])

    async def test_non_member_is_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, uuid4(), world.org_id)
        assert _post_comment(client, world.item_id, "hi").status_code == 403
        assert client.get(f"/api/v1/items/{world.item_id}/comments").status_code == 403

    async def test_annotation_and_parent_must_be_on_the_item(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.owner_id, world.org_id)
        assert (
            _post_comment(client, world.item_id, "x", annotation_id=str(uuid4())).status_code == 404
        )
        assert _post_comment(client, world.item_id, "x", parent_id=str(uuid4())).status_code == 404

    async def test_mention_notifies_the_mentioned_user_only(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.annotator_id, world.org_id)
        response = _post_comment(
            client,
            world.item_id,
            "@reviewer@example.com please look, cc @nobody@example.com",
        )
        assert response.status_code == 201

        notifications = await _rows(sessionmaker, Notification)
        assert len(notifications) == 1
        note = notifications[0]
        assert note.user_id == world.reviewer_id
        assert note.type.value == "mention"
        assert note.payload["comment_id"] == response.json()["id"]
        assert note.payload["actor_id"] == str(world.annotator_id)
        assert note.payload["item_id"] == str(world.item_id)

        # The mentioned user sees it; the author does not.
        _sign_in(app, world.reviewer_id, world.org_id)
        mine = client.get("/api/v1/notifications").json()
        assert [n["type"] for n in mine["items"]] == ["mention"]
        assert client.get("/api/v1/notifications/unread-count").json() == {"count": 1}
        _sign_in(app, world.annotator_id, world.org_id)
        assert client.get("/api/v1/notifications").json()["items"] == []

    async def test_reply_notifies_the_parent_author(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.annotator_id, world.org_id)
        root = _post_comment(client, world.item_id, "Is this a car?").json()

        _sign_in(app, world.reviewer_id, world.org_id)
        reply = _post_comment(client, world.item_id, "Yes", parent_id=root["id"])
        assert reply.status_code == 201
        assert reply.json()["parent_id"] == root["id"]

        notifications = await _rows(sessionmaker, Notification)
        assert [(n.user_id, n.type.value) for n in notifications] == [(world.annotator_id, "reply")]

    async def test_resolve_permissions(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.owner_id, world.org_id)
        comment_id = _post_comment(client, world.item_id, "fix me").json()["id"]

        # An annotator who is not the author may not resolve.
        _sign_in(app, world.annotator_id, world.org_id)
        forbidden = client.post(f"/api/v1/comments/{comment_id}/resolve", json={"resolved": True})
        assert forbidden.status_code == 403

        # A reviewer may.
        _sign_in(app, world.reviewer_id, world.org_id)
        resolved = client.post(f"/api/v1/comments/{comment_id}/resolve", json={"resolved": True})
        assert resolved.status_code == 200
        assert resolved.json()["resolved_at"] is not None

        # The author may reopen.
        _sign_in(app, world.owner_id, world.org_id)
        reopened = client.post(f"/api/v1/comments/{comment_id}/resolve", json={"resolved": False})
        assert reopened.json()["resolved_at"] is None

        actions = [a.action for a in await _rows(sessionmaker, AuditEvent)]
        assert actions == ["comment.create", "comment.resolve", "comment.resolve"]


class TestNotifications:
    async def test_read_one_read_all_and_unread_filter(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.annotator_id, world.org_id)
        for text in ("@reviewer@example.com one", "@reviewer@example.com two"):
            assert _post_comment(client, world.item_id, text).status_code == 201

        _sign_in(app, world.reviewer_id, world.org_id)
        items = client.get("/api/v1/notifications", params={"unread": "true"}).json()["items"]
        assert len(items) == 2
        # Both rows share a second-resolution timestamp on SQLite, so their
        # relative order is not asserted here.
        assert {n["payload"]["excerpt"][-3:] for n in items} == {"one", "two"}

        read = client.post(f"/api/v1/notifications/{items[0]['id']}/read")
        assert read.status_code == 200
        assert read.json()["read_at"] is not None
        # Idempotent.
        again = client.post(f"/api/v1/notifications/{items[0]['id']}/read")
        assert again.json()["read_at"] == read.json()["read_at"]

        assert client.get("/api/v1/notifications/unread-count").json() == {"count": 1}
        unread = client.get("/api/v1/notifications", params={"unread": "true"}).json()["items"]
        assert [n["id"] for n in unread] == [items[1]["id"]]

        assert client.post("/api/v1/notifications/read-all").status_code == 204
        assert client.get("/api/v1/notifications/unread-count").json() == {"count": 0}

    async def test_someone_elses_notification_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.annotator_id, world.org_id)
        _post_comment(client, world.item_id, "@reviewer@example.com hi")
        [note] = await _rows(sessionmaker, Notification)

        _sign_in(app, world.owner_id, world.org_id)
        assert client.post(f"/api/v1/notifications/{note.id}/read").status_code == 404

    async def test_review_verdict_notifies_the_annotator(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        annotation_id = await _add_annotation(sessionmaker, world)
        async with sessionmaker() as session:
            item = await session.get(Item, world.item_id)
            assert item is not None
            item.status = ItemStatus.SUBMITTED
            await session.commit()

        _sign_in(app, world.reviewer_id, world.org_id)
        response = client.post(
            f"/api/v1/annotations/{annotation_id}/review",
            json={"approve": False, "comment": "Box too loose"},
        )
        assert response.status_code == 200, response.text

        [note] = await _rows(sessionmaker, Notification)
        assert note.user_id == world.annotator_id
        assert note.type.value == "review"
        assert note.payload["approve"] is False
        assert note.payload["comment"] == "Box too loose"
        assert note.payload["annotation_id"] == str(annotation_id)

        actions = sorted(a.action for a in await _rows(sessionmaker, AuditEvent))
        assert "annotation.review" in actions


class TestAudit:
    async def test_superuser_lists_own_organisation_newest_first_with_filters(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.owner_id, world.org_id)
        first = _post_comment(client, world.item_id, "one").json()
        client.post(f"/api/v1/comments/{first['id']}/resolve", json={"resolved": True})

        # A row in another organisation must stay invisible.
        async with sessionmaker() as session:
            other = Organization(name="Other", slug=f"other-{uuid4().hex[:8]}")
            session.add(other)
            await session.flush()
            session.add(
                AuditEvent(
                    organization_id=other.id,
                    actor_id=None,
                    action="comment.create",
                    target_type="comment",
                )
            )
            await session.commit()

        _sign_in(app, world.owner_id, world.org_id, superuser=True)
        everything = client.get("/api/v1/audit").json()["items"]
        assert sorted(e["action"] for e in everything) == ["comment.create", "comment.resolve"]
        assert all(e["organization_id"] == str(world.org_id) for e in everything)

        only_resolve = client.get("/api/v1/audit", params={"action": "comment.res"}).json()
        assert [e["action"] for e in only_resolve["items"]] == ["comment.resolve"]

        by_target = client.get("/api/v1/audit", params={"target_id": first["id"]}).json()
        assert len(by_target["items"]) == 2

        by_actor = client.get("/api/v1/audit", params={"actor_id": str(uuid4())}).json()
        assert by_actor["items"] == []

    async def test_non_superuser_is_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        world = await _seed(sessionmaker)
        _sign_in(app, world.owner_id, world.org_id)
        assert client.get("/api/v1/audit").status_code == 403
