"""ML platform endpoints (API-6): registration, check, snapshot publish, version import,
and the Databricks retrain job. The MLflow side is a fake gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from app.main import create_app
from app.models import (
    AuditEvent,
    Membership,
    MlPlatform,
    Model,
    ModelTask,
    ModelVersion,
    Organization,
    Project,
    ProjectRole,
    Snapshot,
    Webhook,
    WebhookDelivery,
)
from app.services import ml_platforms as mp
from app.services.secrets import clear_cache

_TABLES: list[Table] = [
    cast(Table, table.__table__)
    for table in (
        Organization,
        Project,
        Membership,
        AuditEvent,
        Snapshot,
        Webhook,
        WebhookDelivery,
        MlPlatform,
        Model,
        ModelVersion,
    )
]

DIGEST = "ab" * 32


class FakeGateway:
    """Enough of MLflow for the router: experiments, runs, one registry entry."""

    def __init__(self) -> None:
        self.experiments: dict[str, str] = {}
        self.snapshot_runs: dict[tuple[str, str], str] = {}
        self.created: list[dict[str, Any]] = []
        self.runs: dict[str, mp.RunRecord] = {}
        self.registered: dict[tuple[str, str], str] = {}
        self.fail: Exception | None = None
        self.creds: list[mp.HostCreds] = []

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    def experiment_names(self, limit: int) -> list[str]:
        self._maybe_fail()
        return list(self.experiments)[:limit]

    def experiment(self, name: str) -> tuple[str, bool]:
        self._maybe_fail()
        if name in self.experiments:
            return self.experiments[name], False
        self.experiments[name] = str(len(self.experiments) + 1)
        return self.experiments[name], True

    def find_snapshot_run(self, experiment_id: str, snapshot_id: str) -> str | None:
        return self.snapshot_runs.get((experiment_id, snapshot_id))

    def create_snapshot_run(self, experiment_id: str, **kwargs: Any) -> str:
        run_id = f"run{len(self.created) + 1}"
        self.created.append({"experiment_id": experiment_id, **kwargs})
        self.snapshot_runs[(experiment_id, kwargs["tags"][mp.TAG_SNAPSHOT_ID])] = run_id
        return run_id

    def run(self, run_id: str) -> mp.RunRecord:
        self._maybe_fail()
        if run_id not in self.runs:
            raise _NotFoundError(f"Run with id={run_id} not found")
        return self.runs[run_id]

    def registered_run_id(self, name: str, version: str) -> str:
        if (name, version) not in self.registered:
            raise _NotFoundError(f"Registered model {name} version {version} not found")
        return self.registered[(name, version)]


class _NotFoundError(Exception):
    error_code = "RESOURCE_DOES_NOT_EXIST"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway()

    def gateway_for(creds: mp.HostCreds) -> FakeGateway:
        fake.creds.append(creds)
        return fake

    monkeypatch.setattr(mp, "gateway_for", gateway_for)
    clear_cache()
    return fake


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


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, UUID]:
    """An organisation, a project named "Cars" and one of its snapshots."""
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        session.add(org)
        await session.flush()
        project = Project(organization_id=org.id, name="Cars", settings={}, workflow={})
        session.add(project)
        await session.flush()
        snapshot = Snapshot(
            project_id=project.id,
            name="v3",
            filter={},
            label_schema_version_id=uuid4(),
            item_count=42,
            blob_path="snapshots/x/",
            digest=DIGEST,
            created_by_id=uuid4(),
        )
        session.add(snapshot)
        await session.commit()
        return org.id, project.id, snapshot.id


async def _member(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: UUID, user: CurrentUser, role: str
) -> None:
    async with sessionmaker() as session:
        session.add(Membership(user_id=user.id, project_id=project_id, role=ProjectRole(role)))
        await session.commit()


async def _model(sessionmaker: async_sessionmaker[AsyncSession], org_id: UUID) -> UUID:
    async with sessionmaker() as session:
        model = Model(
            organization_id=org_id,
            name="detector",
            task=ModelTask.DETECT,
            endpoint_url="http://model:9000",
            identity_type="none",
        )
        session.add(model)
        await session.commit()
        return model.id


MLFLOW = {"name": "local", "kind": "mlflow", "tracking_uri": "http://mlflow:5000/"}
DATABRICKS = {
    "name": "dbx",
    "kind": "databricks",
    "tracking_uri": "https://adb-1.azuredatabricks.net",
    "identity_type": "bearer",
    "secret_ref": "env:DBX_PAT",
    "config": {"job_id": "42"},
}


def _register(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/v1/ml-platforms", json={"identity_type": "none", **body})
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


class TestRegistration:
    async def test_register_list_get_never_expose_the_secret(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        dbx = _register(client, **DATABRICKS)
        assert local["tracking_uri"] == "http://mlflow:5000"
        assert local["has_secret"] is False
        assert dbx["has_secret"] is True
        assert dbx["config"] == {"job_id": 42}
        assert "secret_ref" not in dbx

        _sign_in(app, _user(org_id))  # any member lists
        listed = client.get("/api/v1/ml-platforms").json()["items"]
        assert [p["name"] for p in listed] == ["dbx", "local"]
        assert client.get(f"/api/v1/ml-platforms/{dbx['id']}").json()["kind"] == "databricks"
        assert client.post("/api/v1/ml-platforms", json=MLFLOW).status_code == 403

        _sign_in(app, _user(uuid4(), is_superuser=True))
        assert client.get(f"/api/v1/ml-platforms/{dbx['id']}").status_code == 404

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({**MLFLOW, "tracking_uri": "mlflow:5000"}, "must start with"),
            ({**DATABRICKS, "identity_type": "none", "secret_ref": None}, "takes identity_type"),
            ({**MLFLOW, "identity_type": "basic"}, "requires a secret_ref"),
            (
                {
                    "name": "aml",
                    "kind": "azureml",
                    "tracking_uri": "azureml://x.api.azureml.ms/mlflow/v1.0/subscriptions/s",
                    "identity_type": "managed_identity",
                    "secret_ref": "env:X",
                },
                "takes no secret_ref",
            ),
            (
                {
                    "name": "aml",
                    "kind": "azureml",
                    "tracking_uri": "azureml://x.api.azureml.ms/mlflow/v1.0/subscriptions/s",
                    "identity_type": "service_principal",
                    "secret_ref": "env:X",
                    "config": {"client_id": "c"},
                },
                "needs config.tenant_id",
            ),
            ({**MLFLOW, "config": {"job_id": 1}}, "unknown config"),
            ({**MLFLOW, "config": {"ui_url": "localhost:5001"}}, "config.ui_url"),
            ({**DATABRICKS, "config": {"job_id": "abc"}}, "positive integer"),
            (
                {**DATABRICKS, "identity_type": "service_principal", "config": {}},
                "needs config.client_id",
            ),
        ],
    )
    async def test_invalid_combinations_are_422(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        body: dict[str, Any],
        message: str,
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        response = client.post("/api/v1/ml-platforms", json={"identity_type": "none", **body})
        assert response.status_code == 422
        assert message in response.text

    async def test_names_are_unique_per_organisation(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        _register(client, **MLFLOW)
        other = _register(client, **{**MLFLOW, "name": "other"})
        duplicate = client.post("/api/v1/ml-platforms", json={"identity_type": "none", **MLFLOW})
        assert duplicate.status_code == 409
        renamed = client.patch(f"/api/v1/ml-platforms/{other['id']}", json={"name": "local"})
        assert renamed.status_code == 409

    async def test_patch_validates_the_merged_row(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(
            client, **{**MLFLOW, "identity_type": "bearer", "secret_ref": "env:ML_TOKEN"}
        )
        url = f"/api/v1/ml-platforms/{local['id']}"
        assert client.patch(url, json={"identity_type": "basic"}).json()["has_secret"] is True
        dropped = client.patch(url, json={"identity_type": "none"}).json()
        assert dropped["has_secret"] is False
        assert client.patch(url, json={"config": {"job_id": 1}}).status_code == 422
        assert client.patch(url, json={"tracking_uri": "https://m.example/"}).json()[
            "tracking_uri"
        ] == ("https://m.example")

    async def test_delete_is_audited(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        assert client.delete(f"/api/v1/ml-platforms/{local['id']}").status_code == 204
        assert client.get(f"/api/v1/ml-platforms/{local['id']}").status_code == 404
        async with sessionmaker() as session:
            actions = set(await session.scalars(select(AuditEvent.action)))
        assert {"ml_platform.create", "ml_platform.delete"} <= actions


class TestCheck:
    async def test_ok_lists_experiments(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        gateway.experiments["Default"] = "0"
        local = _register(client, **MLFLOW)
        result = client.post(f"/api/v1/ml-platforms/{local['id']}/check").json()
        assert result == {
            "ok": True,
            "messages": [],
            "info": {"tracking_uri": "http://mlflow:5000", "experiments": ["Default"]},
        }

    async def test_failures_are_not_ok(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        org_id, _, _ = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        gateway.fail = ConnectionError("connection refused")
        result = client.post(f"/api/v1/ml-platforms/{local['id']}/check").json()
        assert result["ok"] is False
        assert "connection refused" in result["messages"][0]

        monkeypatch.delenv("DBX_PAT", raising=False)
        dbx = _register(client, **DATABRICKS)
        result = client.post(f"/api/v1/ml-platforms/{dbx['id']}/check").json()
        assert result["ok"] is False
        assert "env:DBX_PAT" in result["messages"][0]


class TestPublishSnapshot:
    async def test_owner_publishes_once_then_gets_the_same_run(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        org_id, project_id, snapshot_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        url = f"/api/v1/projects/{project_id}/snapshots/{snapshot_id}/mlflow"

        first = client.post(url, json={"ml_platform_id": local["id"]})
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["experiment_name"] == "annotation/Cars"
        assert body["created"] is True
        assert body["run_url"] == f"http://mlflow:5000/#/experiments/1/runs/{body['run_id']}"
        [created] = gateway.created
        assert created["tags"][mp.TAG_SNAPSHOT_DIGEST] == DIGEST
        assert created["source"]["blob_path"] == "snapshots/x/"
        assert created["profile"]["item_count"] == 42

        again = client.post(url, json={"ml_platform_id": local["id"]})
        assert again.status_code == 200
        assert again.json()["run_id"] == body["run_id"]
        assert again.json()["created"] is False
        assert len(gateway.created) == 1

        other = client.post(url, json={"ml_platform_id": local["id"], "experiment": "mine"})
        assert other.json()["experiment_name"] == "mine"
        assert len(gateway.created) == 2

    async def test_owner_only_and_scoped(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        org_id, project_id, snapshot_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        reviewer = _user(org_id)
        await _member(sessionmaker, project_id, reviewer, "reviewer")
        _sign_in(app, reviewer)
        url = f"/api/v1/projects/{project_id}/snapshots/{snapshot_id}/mlflow"
        assert client.post(url, json={"ml_platform_id": local["id"]}).status_code == 403

        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        missing = f"/api/v1/projects/{project_id}/snapshots/{uuid4()}/mlflow"
        assert client.post(missing, json={"ml_platform_id": local["id"]}).status_code == 404
        assert client.post(url, json={"ml_platform_id": str(uuid4())}).status_code == 404

    async def test_a_failing_platform_is_503(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        org_id, project_id, snapshot_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        gateway.fail = ConnectionError("down")
        response = client.post(
            f"/api/v1/projects/{project_id}/snapshots/{snapshot_id}/mlflow",
            json={"ml_platform_id": local["id"]},
        )
        assert response.status_code == 503
        assert "down" in response.json()["detail"]


def _run(**overrides: Any) -> mp.RunRecord:
    values: dict[str, Any] = {
        "run_id": "r1",
        "experiment_id": "7",
        "name": "train",
        "status": "FINISHED",
        "start_time": 1_700_000_000_000,
        "end_time": 1_700_000_060_000,
        "params": {"epochs": "3"},
        "metrics": {"map50": 0.61},
        "tags": {},
        "datasets": [],
    }
    values.update(overrides)
    return mp.RunRecord(**values)


class TestImportVersion:
    async def _setup(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> tuple[str, str, UUID]:
        org_id, _, snapshot_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        local = _register(client, **MLFLOW)
        model_id = await _model(sessionmaker, org_id)
        return local["id"], f"/api/v1/models/{model_id}/versions/import", snapshot_id

    async def test_a_run_becomes_a_version_with_lineage(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        platform_id, url, snapshot_id = await self._setup(app, client, sessionmaker)
        gateway.runs["r1"] = _run(tags={mp.TAG_SNAPSHOT_DIGEST: DIGEST})

        response = client.post(
            url,
            json={"ml_platform_id": platform_id, "run_id": "r1", "class_mapping": {"car": "car"}},
        )
        assert response.status_code == 201, response.text
        version = response.json()
        assert version["version"] == 1
        assert version["metrics"] == {"map50": 0.61}
        assert version["snapshot_id"] == str(snapshot_id)
        assert version["snapshot_digest"] == DIGEST
        assert version["class_mapping"] == {"car": "car"}
        run = version["training_run"]
        assert run["id"] == "r1"
        assert run["url"] == "http://mlflow:5000/#/experiments/7/runs/r1"
        assert run["params"] == {"epochs": "3"}
        assert run["started_at"].startswith("2023-11-14")
        assert run["source"]["model_uri"] == "runs:/r1"

    async def test_through_a_registered_model_version(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        platform_id, url, _ = await self._setup(app, client, sessionmaker)
        gateway.runs["r1"] = _run()
        gateway.registered[("detector", "4")] = "r1"
        response = client.post(
            url,
            json={
                "ml_platform_id": platform_id,
                "registered_model": "detector",
                "model_version": "4",
            },
        )
        assert response.status_code == 201, response.text
        source = response.json()["training_run"]["source"]
        assert source["model_uri"] == "models:/detector/4"
        assert response.json()["snapshot_id"] is None

        missing = client.post(
            url,
            json={"ml_platform_id": platform_id, "registered_model": "x", "model_version": "1"},
        )
        assert missing.status_code == 404

    async def test_request_shape_is_checked(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        platform_id, url, _ = await self._setup(app, client, sessionmaker)
        assert client.post(url, json={"ml_platform_id": platform_id}).status_code == 422
        both = {"ml_platform_id": platform_id, "run_id": "r", "registered_model": "m"}
        assert client.post(url, json=both).status_code == 422

    async def test_a_digest_that_disagrees_is_409(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        platform_id, url, snapshot_id = await self._setup(app, client, sessionmaker)
        gateway.runs["r1"] = _run(
            tags={mp.TAG_SNAPSHOT_ID: str(snapshot_id), mp.TAG_SNAPSHOT_DIGEST: "cd" * 32}
        )
        response = client.post(url, json={"ml_platform_id": platform_id, "run_id": "r1"})
        assert response.status_code == 409

    async def test_malformed_or_foreign_lineage(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        platform_id, url, _ = await self._setup(app, client, sessionmaker)
        gateway.runs["bad"] = _run(run_id="bad", tags={mp.TAG_SNAPSHOT_DIGEST: "short"})
        gateway.runs["foreign"] = _run(run_id="foreign", tags={mp.TAG_SNAPSHOT_ID: str(uuid4())})
        bad = client.post(url, json={"ml_platform_id": platform_id, "run_id": "bad"})
        assert bad.status_code == 422
        foreign = client.post(url, json={"ml_platform_id": platform_id, "run_id": "foreign"})
        assert foreign.status_code == 404
        assert "not in this organisation" in foreign.json()["detail"]

    async def test_missing_run_is_404_and_a_failing_platform_503(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: FakeGateway,
    ) -> None:
        platform_id, url, _ = await self._setup(app, client, sessionmaker)
        assert (
            client.post(url, json={"ml_platform_id": platform_id, "run_id": "nope"}).status_code
            == 404
        )
        gateway.fail = ConnectionError("down")
        assert (
            client.post(url, json={"ml_platform_id": platform_id, "run_id": "r1"}).status_code
            == 503
        )


class TestRetrainOnDatabricks:
    async def _owner(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> tuple[UUID, UUID, dict[str, Any], dict[str, Any]]:
        org_id, project_id, snapshot_id = await _seed(sessionmaker)
        _sign_in(app, _user(org_id, is_superuser=True))
        dbx = _register(client, **DATABRICKS)
        local = _register(client, **MLFLOW)
        owner = _user(org_id)
        await _member(sessionmaker, project_id, owner, "owner")
        _sign_in(app, owner)
        client.post(
            "/api/v1/webhooks",
            json={
                "url": "https://hooks.example/in",
                "project_id": str(project_id),
                "events": ["retrain.requested"],
            },
        )
        return project_id, snapshot_id, dbx, local

    async def test_the_job_starts_before_the_event(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_id, snapshot_id, dbx, _ = await self._owner(app, client, sessionmaker)
        monkeypatch.setenv("DBX_PAT", "pat")
        clear_cache()
        started: list[tuple[int, dict[str, str]]] = []

        def start(creds: mp.HostCreds, job_id: int, parameters: dict[str, str]) -> str:
            assert creds.token == "pat"
            started.append((job_id, parameters))
            return "777"

        monkeypatch.setattr(mp, "start_databricks_job", start)
        response = client.post(
            f"/api/v1/projects/{project_id}/retrain",
            json={"snapshot_id": str(snapshot_id), "ml_platform_id": dbx["id"], "note": "go"},
        )
        assert response.status_code == 202, response.text
        ml_run = {
            "ml_platform_id": dbx["id"],
            "run_id": "777",
            "run_url": "https://adb-1.azuredatabricks.net/jobs/42/runs/777",
        }
        assert response.json() == {
            "event": "retrain.requested",
            "deliveries": 1,
            "ml_run": ml_run,
        }
        [(job_id, parameters)] = started
        assert job_id == 42
        assert parameters == {
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "snapshot_digest": DIGEST,
            "snapshot_blob_path": "snapshots/x/",
            "model_id": "",
            "note": "go",
        }
        async with sessionmaker() as session:
            [delivery] = list(await session.scalars(select(WebhookDelivery)))
        data = delivery.payload["data"]
        assert isinstance(data, dict)
        assert data["ml_run"] == ml_run

    async def test_other_platforms_are_422_and_a_failed_start_emits_nothing(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_id, _, dbx, local = await self._owner(app, client, sessionmaker)
        url = f"/api/v1/projects/{project_id}/retrain"
        assert client.post(url, json={"ml_platform_id": local["id"]}).status_code == 422
        assert client.post(url, json={"ml_platform_id": str(uuid4())}).status_code == 404

        monkeypatch.setenv("DBX_PAT", "pat")
        clear_cache()

        def fail(creds: mp.HostCreds, job_id: int, parameters: dict[str, str]) -> str:
            raise ConnectionError("workspace unreachable")

        monkeypatch.setattr(mp, "start_databricks_job", fail)
        response = client.post(url, json={"ml_platform_id": dbx["id"]})
        assert response.status_code == 503
        assert "workspace unreachable" in response.json()["detail"]
        async with sessionmaker() as session:
            assert list(await session.scalars(select(WebhookDelivery))) == []
