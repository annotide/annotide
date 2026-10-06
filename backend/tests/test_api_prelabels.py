"""`POST /items/{id}/prelabels` (API-8): an external producer's pre-label.

Same in-memory SQLite setup as `tests/test_api_annotations.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

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
    AnnotationSource,
    AnnotationStatus,
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    ItemStatus,
    LabelSchema,
    LabelSchemaVersion,
    MediaType,
    Membership,
    Model,
    ModelTask,
    ModelVersion,
    Organization,
    OutboxEvent,
    Project,
    ProjectRole,
)
from app.services.queue import get_job_queue
from tests.support import FakeJobQueue

_TABLES: list[Table] = [
    cast(Table, model.__table__)
    for model in (
        AuditEvent,
        Organization,
        Connector,
        Project,
        Membership,
        Item,
        Annotation,
        LabelSchema,
        LabelSchemaVersion,
        OutboxEvent,
        Model,
        ModelVersion,
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
def client(sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def _get_queue() -> FakeJobQueue:
        return FakeJobQueue()

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_job_queue] = _get_queue
    return TestClient(application, raise_server_exceptions=False)


class World:
    """An organisation with a project, an item, an agent model and its member."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def build(self, role: ProjectRole = ProjectRole.ANNOTATOR) -> World:
        async with self.sessionmaker() as session:
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
            project = Project(organization_id=org.id, name="P")
            session.add_all([connector, project])
            await session.flush()
            schema = LabelSchema(project_id=project.id, name="traffic")
            session.add(schema)
            await session.flush()
            schema_version = LabelSchemaVersion(
                label_schema_id=schema.id, version=1, definition=DEMO_SCHEMA
            )
            session.add(schema_version)
            project.label_schema_id = schema.id
            item = Item(
                project_id=project.id,
                connector_id=connector.id,
                path="a.jpg",
                media_type=MediaType.IMAGE,
                size_bytes=1,
                meta={},
                status=ItemStatus.NEW,
            )
            # An external producer: no endpoint.
            model = Model(
                organization_id=org.id,
                name="Claude agent",
                task=ModelTask.DETECT,
                endpoint_url=None,
                identity_type="none",
            )
            session.add_all([item, model])
            await session.flush()
            version = ModelVersion(model_id=model.id, version=1, class_mapping={}, metrics={})
            session.add(version)
            await session.flush()
            self.user = CurrentUser(
                id=uuid4(),
                organization_id=org.id,
                email="svc-agent@example.com",
                is_superuser=False,
                is_service=True,
                scopes=frozenset({"write"}),
            )
            session.add(Membership(user_id=self.user.id, project_id=project.id, role=role))
            await session.commit()
            self.org_id = org.id
            self.project_id = project.id
            self.item_id = item.id
            self.model_id = model.id
            self.version_id = version.id
            self.schema_version_id = schema_version.id
            self.connector_id = connector.id
        return self

    def sign_in(self, client: TestClient) -> None:
        app = cast(FastAPI, client.app)
        app.dependency_overrides[get_current_user] = lambda: self.user


def _result() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "media_type": "image",
        "classification": {},
        "shapes": [{"id": str(uuid4()), "type": "bbox", "class": "car", "bbox": [1, 2, 30, 40]}],
    }


def _post(client: TestClient, world: World, **body: Any) -> Any:
    return client.post(
        f"/api/v1/items/{world.item_id}/prelabels",
        json={"model_version_id": str(world.version_id), "result": _result(), **body},
    )


async def test_writes_a_model_draft_and_marks_the_item_prelabeled(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build()
    world.sign_in(client)

    response = _post(client, world)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "model"
    assert body["status"] == "draft"
    assert body["author_model_version_id"] == str(world.version_id)
    assert body["author_user_id"] is None
    assert body["label_schema_version_id"] == str(world.schema_version_id)
    async with sessionmaker() as session:
        item = await session.get(Item, world.item_id)
        assert item is not None and item.status is ItemStatus.PRELABELED
        actions = list(await session.scalars(select(AuditEvent.action)))
        assert actions == ["annotation.prelabel"]
    # A second pre-label is a new version on top, still not human work.
    assert _post(client, world).json()["version"] == 2


async def test_never_over_human_work(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build()
    world.sign_in(client)
    async with sessionmaker() as session:
        session.add(
            Annotation(
                item_id=world.item_id,
                version=1,
                author_user_id=uuid4(),
                source=AnnotationSource.HUMAN,
                label_schema_version_id=world.schema_version_id,
                result=_result(),
                status=AnnotationStatus.DRAFT,
            )
        )
        await session.commit()

    response = _post(client, world)
    assert response.status_code == 409
    assert "human annotation" in response.json()["detail"]


@pytest.mark.parametrize("status", [ItemStatus.SUBMITTED, ItemStatus.APPROVED])
async def test_not_once_the_item_has_moved_on(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession], status: ItemStatus
) -> None:
    world = await World(sessionmaker).build()
    world.sign_in(client)
    async with sessionmaker() as session:
        item = await session.get(Item, world.item_id)
        assert item is not None
        item.status = status
        await session.commit()

    assert _post(client, world).status_code == 409


async def test_reviewers_and_viewers_may_not(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build(role=ProjectRole.REVIEWER)
    world.sign_in(client)
    assert _post(client, world).status_code == 403


async def test_another_organisations_model_version_is_not_found(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build()
    other = await World(sessionmaker).build()
    world.sign_in(client)

    response = _post(client, world, model_version_id=str(other.version_id))
    assert response.status_code == 404


async def test_a_schema_version_of_another_project_is_refused(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build()
    other = await World(sessionmaker).build()
    world.sign_in(client)

    response = _post(client, world, label_schema_version_id=str(other.schema_version_id))
    assert response.status_code == 409


async def test_an_external_producer_cannot_run_a_prelabel_job(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await World(sessionmaker).build(role=ProjectRole.OWNER)
    world.sign_in(client)

    response = client.post(
        f"/api/v1/projects/{world.project_id}/prelabel",
        json={"model_version_id": str(world.version_id)},
    )
    assert response.status_code == 409
    assert "no endpoint" in response.json()["detail"]


def test_a_model_may_be_registered_without_an_endpoint() -> None:
    from app.schemas.model import ModelCreate

    model = ModelCreate(name="agent", task="detect")
    assert model.endpoint_url is None
    with pytest.raises(ValueError, match="http"):
        ModelCreate(name="x", task="detect", endpoint_url="ftp://x")


async def test_the_model_client_refuses_an_external_producer() -> None:
    from app.services.models import ModelClient, ModelUnavailable

    with pytest.raises(ModelUnavailable, match="no endpoint"):
        await ModelClient.for_model(None, "none", None)
