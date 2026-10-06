"""Tests for the `/models` router (ML-1, BYOM-2, BYOM-3).

No live database: each test gets a fresh in-memory SQLite database (an async
engine on a `StaticPool`, so the same connection survives across the
requests a single test makes) with only the `model` / `model_version` tables,
and `app.api.deps.get_current_user` is overridden with a fake `CurrentUser`
instead of decoding a real JWT.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationSource,
    AuditEvent,
    Item,
    MediaType,
    Model,
    ModelTask,
    ModelVersion,
    Project,
    Snapshot,
)
from app.services.models import ModelRejected, ModelUnavailable

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [
        AuditEvent.__table__,
        Model.__table__,
        ModelVersion.__table__,
        Project.__table__,
        Item.__table__,
        Annotation.__table__,
        Snapshot.__table__,
    ],
)

DIGEST = "ab" * 32


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
    user_id: uuid.UUID = ADMIN_ID,
    organization_id: uuid.UUID = ORG_ID,
    is_superuser: bool = True,
) -> None:
    user = CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="admin@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
    name: str = "detector",
    task: ModelTask = ModelTask.DETECT,
    endpoint_url: str = "http://model.example.com",
    identity_type: str = "none",
    secret_ref: str | None = None,
    created_at: datetime | None = None,
) -> Model:
    moment = created_at or datetime.now(UTC)

    async def _create() -> Model:
        async with sessionmaker() as session:
            model = Model(
                organization_id=organization_id,
                name=name,
                task=task,
                endpoint_url=endpoint_url,
                identity_type=identity_type,
                secret_ref=secret_ref,
                created_at=moment,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            return model

    return _run(_create())


def _seed_version(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    model_id: uuid.UUID,
    version: int,
    class_mapping: dict[str, str | None] | None = None,
    metrics: dict[str, Any] | None = None,
) -> ModelVersion:
    async def _create() -> ModelVersion:
        async with sessionmaker() as session:
            model_version = ModelVersion(
                model_id=model_id,
                version=version,
                class_mapping=class_mapping or {},
                metrics=metrics or {},
            )
            session.add(model_version)
            await session.commit()
            await session.refresh(model_version)
            return model_version

    return _run(_create())


def _seed_model_annotation(
    sessionmaker: async_sessionmaker[AsyncSession], version_id: uuid.UUID
) -> uuid.UUID:
    """A draft pre-label written by `version_id`, on an item of a new project."""

    async def _create() -> uuid.UUID:
        async with sessionmaker() as session:
            project = Project(organization_id=ORG_ID, name="proj", settings={}, workflow={})
            session.add(project)
            await session.flush()
            item = Item(
                project_id=project.id,
                connector_id=uuid.uuid4(),
                path="a.jpg",
                media_type=MediaType.IMAGE,
                size_bytes=1,
            )
            session.add(item)
            await session.flush()
            annotation = Annotation(
                item_id=item.id,
                version=1,
                author_model_version_id=version_id,
                source=AnnotationSource.MODEL,
                label_schema_version_id=uuid.uuid4(),
                result={"shapes": []},
            )
            session.add(annotation)
            await session.commit()
            return annotation.id

    return _run(_create())


def _delete_mode(app: FastAPI, mode: Literal["soft", "hard"]) -> None:
    settings = get_settings().model_copy(update={"model_delete_mode": mode})
    app.dependency_overrides[get_settings] = lambda: settings


def _seed_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
    digest: str = DIGEST,
) -> Snapshot:
    async def _create() -> Snapshot:
        async with sessionmaker() as session:
            project = Project(
                organization_id=organization_id, name="proj", settings={}, workflow={}
            )
            session.add(project)
            await session.flush()
            snapshot = Snapshot(
                project_id=project.id,
                name="v1",
                filter={},
                label_schema_version_id=uuid.uuid4(),
                item_count=3,
                blob_path=f"snapshots/{uuid.uuid4()}/",
                digest=digest,
                created_by_id=ADMIN_ID,
            )
            session.add(snapshot)
            await session.commit()
            await session.refresh(snapshot)
            return snapshot

    return _run(_create())


class TestList:
    def test_list_scoped_to_organization(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _seed_model(sessionmaker, organization_id=ORG_ID, name="mine")
        _seed_model(sessionmaker, organization_id=OTHER_ORG_ID, name="not-mine")
        _login(app, is_superuser=False)

        response = client.get("/api/v1/models")
        assert response.status_code == 200
        names = [item["name"] for item in response.json()["items"]]
        assert names == ["mine"]

    def test_401_without_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/models")
        assert response.status_code == 401


class TestCreateAndRead:
    def test_403_for_non_superuser(self, app: FastAPI, client: TestClient) -> None:
        _login(app, is_superuser=False)
        response = client.post(
            "/api/v1/models",
            json={
                "name": "detector",
                "task": "detect",
                "endpoint_url": "http://model.example.com",
            },
        )
        assert response.status_code == 403

    def test_create_and_get_round_trip_never_expose_secret_ref(
        self, app: FastAPI, client: TestClient
    ) -> None:
        _login(app)
        response = client.post(
            "/api/v1/models",
            json={
                "name": "detector",
                "task": "detect",
                "endpoint_url": "http://model.example.com",
                "identity_type": "api_key",
                "secret_ref": "MODEL_API_KEY",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["has_secret"] is True
        assert "secret_ref" not in body

        fetched = client.get(f"/api/v1/models/{body['id']}")
        assert fetched.status_code == 200
        assert "secret_ref" not in fetched.json()
        assert fetched.json()["has_secret"] is True

    def test_has_secret_false_without_one(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        response = client.post(
            "/api/v1/models",
            json={
                "name": "detector",
                "task": "classify",
                "endpoint_url": "http://model.example.com",
            },
        )
        assert response.status_code == 201
        assert response.json()["has_secret"] is False

    def test_api_key_without_secret_ref_is_422(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        response = client.post(
            "/api/v1/models",
            json={
                "name": "detector",
                "task": "detect",
                "endpoint_url": "http://model.example.com",
                "identity_type": "api_key",
            },
        )
        assert response.status_code == 422

    def test_404_for_another_organizations_model(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app, organization_id=ORG_ID, is_superuser=False)

        response = client.get(f"/api/v1/models/{model.id}")
        assert response.status_code == 404


class TestEntraIdentities:
    """Managed identity and service principal (BYOM-3)."""

    _BASE: ClassVar[dict[str, Any]] = {
        "name": "aml",
        "task": "detect",
        "endpoint_url": "https://aml.example.inference.ml.azure.com",
    }

    def test_managed_identity_needs_no_secret(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        config = {"scope": "https://ml.azure.com/.default", "client_id": "uami-client"}
        response = client.post(
            "/api/v1/models",
            json={**self._BASE, "identity_type": "managed_identity", "identity_config": config},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["has_secret"] is False
        assert body["identity_config"] == {**config, "tenant_id": None}

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ({"identity_type": "managed_identity"}, "identity_config.scope"),
            (
                {
                    "identity_type": "managed_identity",
                    "secret_ref": "env:KEY",
                    "identity_config": {"scope": "https://ml.azure.com/.default"},
                },
                "takes no secret_ref",
            ),
            (
                {
                    "identity_type": "service_principal",
                    "secret_ref": "env:SP_SECRET",
                    "identity_config": {"scope": "https://ml.azure.com/.default"},
                },
                "tenant_id, client_id",
            ),
            (
                {
                    "identity_type": "managed_identity",
                    "identity_config": {"scope": "https://ml.azure.com"},
                },
                "/.default",
            ),
        ],
    )
    def test_incomplete_identities_are_422(
        self, app: FastAPI, client: TestClient, extra: dict[str, Any], message: str
    ) -> None:
        _login(app)
        response = client.post("/api/v1/models", json={**self._BASE, **extra})

        assert response.status_code == 422
        assert message in response.text

    def test_switching_to_managed_identity_replaces_the_secret(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker, identity_type="api_key", secret_ref="env:KEY")
        _login(app)
        switch = {
            "identity_type": "managed_identity",
            "identity_config": {"scope": "https://cognitiveservices.azure.com/.default"},
        }

        kept_secret = client.patch(f"/api/v1/models/{model.id}", json=switch)
        assert kept_secret.status_code == 422

        response = client.patch(f"/api/v1/models/{model.id}", json={**switch, "secret_ref": None})
        assert response.status_code == 200, response.text
        assert response.json()["has_secret"] is False
        assert response.json()["identity_config"]["scope"].startswith("https://cognitive")

        back = client.patch(
            f"/api/v1/models/{model.id}",
            json={"identity_type": "bearer", "secret_ref": "env:T", "identity_config": None},
        )
        assert back.status_code == 200
        assert back.json()["identity_config"] == {
            "scope": None,
            "tenant_id": None,
            "client_id": None,
        }


class TestUpdateAndDelete:
    def test_patch_updates_only_given_fields(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker, name="old-name")
        _login(app)

        response = client.patch(f"/api/v1/models/{model.id}", json={"name": "new-name"})
        assert response.status_code == 200
        assert response.json()["name"] == "new-name"

    def test_delete_is_soft_by_default(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        version = _seed_version(sessionmaker, model_id=model.id, version=1)
        annotation_id = _seed_model_annotation(sessionmaker, version.id)
        _login(app)

        response = client.delete(f"/api/v1/models/{model.id}")
        assert response.status_code == 204

        # Gone from the API: the model, its versions and the list.
        assert client.get(f"/api/v1/models/{model.id}").status_code == 404
        assert client.get(f"/api/v1/models/{model.id}/versions").status_code == 404
        assert client.get(f"/api/v1/models/{model.id}/versions/{version.id}").status_code == 404
        assert client.get("/api/v1/models").json()["items"] == []
        assert client.delete(f"/api/v1/models/{model.id}").status_code == 404

        # The rows stay, so the pre-label keeps its author.
        async def _rows() -> tuple[Model | None, Annotation | None, AuditEvent | None]:
            async with sessionmaker() as session:
                audit = await session.scalar(
                    select(AuditEvent).where(AuditEvent.action == "model.delete")
                )
                return (
                    await session.get(Model, model.id),
                    await session.get(Annotation, annotation_id),
                    audit,
                )

        row, annotation, audit = _run(_rows())
        assert row is not None
        assert row.deleted_at is not None
        assert annotation is not None
        assert annotation.author_model_version_id == version.id
        assert audit is not None
        assert audit.after == {"mode": "soft"}

    def test_hard_delete_removes_a_model_that_wrote_nothing(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _seed_version(sessionmaker, model_id=model.id, version=1)
        _login(app)
        _delete_mode(app, "hard")

        assert client.delete(f"/api/v1/models/{model.id}").status_code == 204

        # The versions go with it by ON DELETE CASCADE, which this SQLite
        # engine doesn't enforce; Postgres does.
        async def _row() -> Model | None:
            async with sessionmaker() as session:
                return await session.get(Model, model.id)

        assert _run(_row()) is None

    def test_hard_delete_is_refused_while_annotations_name_a_version(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        version = _seed_version(sessionmaker, model_id=model.id, version=1)
        _seed_model_annotation(sessionmaker, version.id)
        _login(app)
        _delete_mode(app, "hard")

        response = client.delete(f"/api/v1/models/{model.id}")
        assert response.status_code == 409
        assert "APP_MODEL_DELETE_MODE=soft" in response.json()["detail"]
        assert client.get(f"/api/v1/models/{model.id}").status_code == 200

    def test_403_for_non_superuser(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app, is_superuser=False)

        response = client.patch(f"/api/v1/models/{model.id}", json={"name": "nope"})
        assert response.status_code == 403


class TestCheck:
    def test_ok_returns_info(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        class _StubClient:
            async def info(self) -> dict[str, Any]:
                return {"classes": ["cat", "dog"]}

            async def aclose(self) -> None:
                pass

        async def _for_model(*args: Any, **kwargs: Any) -> _StubClient:
            return _StubClient()

        monkeypatch.setattr("app.api.v1.models.ModelClient.for_model", _for_model)

        response = client.post(f"/api/v1/models/{model.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["info"] == {"classes": ["cat", "dog"]}

    def test_model_unavailable_is_not_ok(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        async def _for_model(*args: Any, **kwargs: Any) -> Any:
            raise ModelUnavailable("boom")

        monkeypatch.setattr("app.api.v1.models.ModelClient.for_model", _for_model)

        response = client.post(f"/api/v1/models/{model.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "boom" in body["messages"][0]

    def test_model_rejected_is_not_ok(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        async def _for_model(*args: Any, **kwargs: Any) -> Any:
            raise ModelRejected("nope")

        monkeypatch.setattr("app.api.v1.models.ModelClient.for_model", _for_model)

        response = client.post(f"/api/v1/models/{model.id}/check")
        assert response.status_code == 200
        assert response.json()["ok"] is False

    def test_403_for_non_superuser(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app, is_superuser=False)

        response = client.post(f"/api/v1/models/{model.id}/check")
        assert response.status_code == 403


class TestVersions:
    def test_auto_numbering(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        versions = []
        for _ in range(3):
            response = client.post(f"/api/v1/models/{model.id}/versions", json={})
            assert response.status_code == 201
            versions.append(response.json()["version"])

        assert versions == [1, 2, 3]

    def test_explicit_duplicate_version_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _seed_version(sessionmaker, model_id=model.id, version=1)
        _login(app)

        response = client.post(f"/api/v1/models/{model.id}/versions", json={"version": 1})
        assert response.status_code == 409

    def test_invalid_mapping_value_is_422(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions",
            json={"class_mapping": {"person": 5}},
        )
        assert response.status_code == 422

    def test_list_ordered_ascending(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _seed_version(sessionmaker, model_id=model.id, version=2)
        _seed_version(sessionmaker, model_id=model.id, version=1)
        _login(app, is_superuser=False)

        response = client.get(f"/api/v1/models/{model.id}/versions")
        assert response.status_code == 200
        assert [item["version"] for item in response.json()["items"]] == [1, 2]

    def test_get_version(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        version = _seed_version(
            sessionmaker, model_id=model.id, version=1, class_mapping={"cat": "animal"}
        )
        _login(app, is_superuser=False)

        response = client.get(f"/api/v1/models/{model.id}/versions/{version.id}")
        assert response.status_code == 200
        assert response.json()["class_mapping"] == {"cat": "animal"}

    def test_get_version_wrong_model_is_404(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model_a = _seed_model(sessionmaker, name="a")
        model_b = _seed_model(sessionmaker, name="b")
        version = _seed_version(sessionmaker, model_id=model_a.id, version=1)
        _login(app, is_superuser=False)

        response = client.get(f"/api/v1/models/{model_b.id}/versions/{version.id}")
        assert response.status_code == 404

    def test_403_for_non_superuser_create(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app, is_superuser=False)

        response = client.post(f"/api/v1/models/{model.id}/versions", json={})
        assert response.status_code == 403

    def test_metrics_for_a_version_with_no_drafts_are_empty(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """ML-5: the route answers even before the version has pre-labelled anything."""
        model = _seed_model(sessionmaker)
        version = _seed_version(sessionmaker, model_id=model.id, version=1)
        _login(app, is_superuser=False)

        response = client.get(f"/api/v1/models/{model.id}/versions/{version.id}/metrics")

        assert response.status_code == 200
        body = response.json()
        assert body["model_version_id"] == str(version.id)
        assert body["project_id"] is None
        assert body["items_predicted"] == 0
        assert body["precision"] is None
        assert body["classes"] == []

    def test_metrics_404_for_wrong_model_or_foreign_project(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model_a = _seed_model(sessionmaker, name="a")
        model_b = _seed_model(sessionmaker, name="b")
        version = _seed_version(sessionmaker, model_id=model_a.id, version=1)
        _login(app, is_superuser=False)

        wrong_model = client.get(f"/api/v1/models/{model_b.id}/versions/{version.id}/metrics")
        assert wrong_model.status_code == 404

        unknown_project = client.get(
            f"/api/v1/models/{model_a.id}/versions/{version.id}/metrics",
            params={"project_id": str(uuid.uuid4())},
        )
        assert unknown_project.status_code == 404


class TestVersionLineage:
    """EXP-8: a version records the snapshot it was trained on."""

    def test_snapshot_id_fills_in_the_digest_and_stores_the_run(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions",
            json={
                "snapshot_id": str(snapshot.id),
                "training_run": {"id": "run-7", "url": "https://ci.example.com/run/7"},
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["snapshot_id"] == str(snapshot.id)
        assert body["snapshot_digest"] == DIGEST
        assert body["training_run"] == {"id": "run-7", "url": "https://ci.example.com/run/7"}

        read = client.get(f"/api/v1/models/{model.id}/versions/{body['id']}")
        assert read.json()["snapshot_digest"] == DIGEST

    def test_digest_that_disagrees_with_the_snapshot_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions",
            json={"snapshot_id": str(snapshot.id), "snapshot_digest": "cd" * 32},
        )

        assert response.status_code == 409
        assert "does not match" in response.json()["detail"]

    def test_digest_alone_links_the_matching_snapshot(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker)
        _login(app)

        linked = client.post(
            f"/api/v1/models/{model.id}/versions", json={"snapshot_digest": DIGEST}
        )
        assert linked.status_code == 201, linked.text
        assert linked.json()["snapshot_id"] == str(snapshot.id)

        unknown = client.post(
            f"/api/v1/models/{model.id}/versions", json={"snapshot_digest": "ef" * 32}
        )
        assert unknown.status_code == 201, unknown.text
        assert unknown.json()["snapshot_id"] is None
        assert unknown.json()["snapshot_digest"] == "ef" * 32

    def test_snapshot_of_another_organisation_is_404(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        foreign = _seed_snapshot(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions", json={"snapshot_id": str(foreign.id)}
        )
        assert response.status_code == 404

        by_digest = client.post(
            f"/api/v1/models/{model.id}/versions", json={"snapshot_digest": DIGEST}
        )
        assert by_digest.status_code == 201
        assert by_digest.json()["snapshot_id"] is None

    def test_malformed_digest_is_422(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions", json={"snapshot_digest": "not-a-digest"}
        )
        assert response.status_code == 422


class TestVersionDerivation:
    """EXP-8: a version names the version it was distilled or quantized from."""

    def test_quantized_child_records_its_parent(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        parent = _seed_version(sessionmaker, model_id=model.id, version=1)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions",
            json={
                "parent_version_id": str(parent.id),
                "derivation": "quantized",
                "metrics": {"dtype": "int8", "size_bytes": 1024},
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["parent_version_id"] == str(parent.id)
        assert body["derivation"] == "quantized"
        read = client.get(f"/api/v1/models/{model.id}/versions/{body['id']}")
        assert read.json()["derivation"] == "quantized"

    def test_distilled_without_a_parent_is_422(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions", json={"derivation": "distilled"}
        )
        assert response.status_code == 422

    def test_trained_without_a_parent_is_fine(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions", json={"derivation": "trained"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["parent_version_id"] is None

    def test_parent_in_another_organisation_is_404(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        model = _seed_model(sessionmaker)
        foreign = _seed_model(sessionmaker, organization_id=OTHER_ORG_ID, name="theirs")
        foreign_version = _seed_version(sessionmaker, model_id=foreign.id, version=1)
        _login(app)

        response = client.post(
            f"/api/v1/models/{model.id}/versions",
            json={"parent_version_id": str(foreign_version.id), "derivation": "distilled"},
        )
        assert response.status_code == 404

    def test_family_spans_models_and_leaves_out_unrelated_versions(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        teacher_model = _seed_model(sessionmaker, name="teacher")
        student_model = _seed_model(sessionmaker, name="student")
        unrelated_model = _seed_model(sessionmaker, name="unrelated")
        teacher = _seed_version(sessionmaker, model_id=teacher_model.id, version=1)
        _seed_version(sessionmaker, model_id=unrelated_model.id, version=1)
        _login(app)

        student = client.post(
            f"/api/v1/models/{student_model.id}/versions",
            json={"parent_version_id": str(teacher.id), "derivation": "distilled"},
        ).json()
        quantized = client.post(
            f"/api/v1/models/{student_model.id}/versions",
            json={"parent_version_id": student["id"], "derivation": "quantized"},
        ).json()

        # From the teacher's model: the student and its int8 copy, two hops away.
        response = client.get(f"/api/v1/models/{teacher_model.id}/family")
        assert response.status_code == 200, response.text
        versions = response.json()["versions"]
        assert {v["id"] for v in versions} == {str(teacher.id), student["id"], quantized["id"]}
        by_id = {v["id"]: v for v in versions}
        assert by_id[str(teacher.id)]["model_name"] == "teacher"
        assert by_id[quantized["id"]]["parent_version_id"] == student["id"]
        assert by_id[student["id"]]["model_task"] == "detect"

        # From the unrelated model: only its own version.
        alone = client.get(f"/api/v1/models/{unrelated_model.id}/family").json()["versions"]
        assert [v["model_name"] for v in alone] == ["unrelated"]

    def test_family_of_a_model_in_another_organisation_is_404(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        foreign = _seed_model(sessionmaker, organization_id=OTHER_ORG_ID, name="theirs")
        _login(app)

        assert client.get(f"/api/v1/models/{foreign.id}/family").status_code == 404
