"""Tests for the jobs router (`/jobs/{id}`, `/jobs/{id}/download`,
`/projects/{id}/jobs`, `/projects/{id}/exports`, `/projects/{id}/scan`).

No live database: each test gets a fresh in-memory SQLite database (an async
engine on a `StaticPool`, so the same connection survives across the
requests a single test makes) with only the `project`, `connector`, `job` and
`membership` tables, and `app.api.deps.get_current_user` is overridden with
a fake `CurrentUser` instead of decoding a real JWT. The job queue is a
`FakeJobQueue` that records what would have gone to Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.connectors.errors import ConnectorError
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
    JobStatus,
    JobType,
    Membership,
    Model,
    ModelTask,
    ModelVersion,
    Project,
    ProjectRole,
)
from app.services.queue import QueueUnavailableError, get_job_queue
from tests.support import FakeJobQueue

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
MEMBER_ID = uuid.uuid4()
OUTSIDER_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [
        AuditEvent.__table__,
        IdempotencyKey.__table__,
        Project.__table__,
        Connector.__table__,
        Job.__table__,
        Membership.__table__,
        Model.__table__,
        ModelVersion.__table__,
    ],
)


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
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession], queue: FakeJobQueue) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def _get_queue() -> FakeJobQueue:
        return queue

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_job_queue] = _get_queue
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _login(
    app: FastAPI,
    *,
    user_id: uuid.UUID = MEMBER_ID,
    organization_id: uuid.UUID = ORG_ID,
    is_superuser: bool = False,
) -> None:
    user = CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="member@example.com",
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
    result_connector_id: uuid.UUID | None = None,
) -> Project:
    moment = datetime.now(UTC)

    async def _create() -> Project:
        async with sessionmaker() as session:
            project = Project(
                organization_id=organization_id,
                name=name,
                result_connector_id=result_connector_id,
                settings={},
                workflow={},
                created_at=moment,
                updated_at=moment,
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            return project

    return _run(_create())


def _seed_local_connector(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    root: Path,
    organization_id: uuid.UUID = ORG_ID,
) -> Connector:
    """A `local` connector rooted at `root`; signs URLs without any cloud SDK."""

    async def _create() -> Connector:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=organization_id,
                name="results",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                config={"root": str(root)},
            )
            session.add(connector)
            await session.commit()
            await session.refresh(connector)
            return connector

    return _run(_create())


def _seed_membership(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID = MEMBER_ID,
    role: ProjectRole = ProjectRole.ANNOTATOR,
) -> None:
    async def _create() -> None:
        async with sessionmaker() as session:
            session.add(Membership(user_id=user_id, project_id=project_id, role=role))
            await session.commit()

    _run(_create())


def _seed_model_version(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
) -> ModelVersion:
    """A registered model with one version, in `organization_id`."""

    async def _create() -> ModelVersion:
        async with sessionmaker() as session:
            model = Model(
                organization_id=organization_id,
                name="detector",
                task=ModelTask.DETECT,
                endpoint_url="http://model.example",
                identity_type="none",
                secret_ref=None,
            )
            session.add(model)
            await session.flush()
            version = ModelVersion(model_id=model.id, version=1, class_mapping={}, metrics={})
            session.add(version)
            await session.commit()
            await session.refresh(version)
            return version

    return _run(_create())


def _seed_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID | None,
    type_: JobType = JobType.EXPORT,
    status: JobStatus = JobStatus.QUEUED,
    result: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> Job:
    moment = created_at or datetime.now(UTC)

    async def _create() -> Job:
        async with sessionmaker() as session:
            job = Job(
                project_id=project_id,
                type=type_,
                status=status,
                payload={},
                result=result,
                created_at=moment,
                updated_at=moment,
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    return _run(_create())


class TestGetJob:
    def test_get_job_through_project_membership(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id)
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(job.id)

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, user_id=MEMBER_ID)
        job = _seed_job(sessionmaker, project_id=project.id)
        _login(app, user_id=OUTSIDER_ID)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 403

    def test_404_for_another_organizations_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, organization_id=OTHER_ORG_ID)
        job = _seed_job(sessionmaker, project_id=project.id)
        _login(app, organization_id=ORG_ID)

        response = client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 404

    def test_401_without_token(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert response.status_code == 401


class TestDownloadExport:
    def test_signs_a_url_for_a_succeeded_export(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Any,
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id)
        blob_path = "exports/some-job/coco.zip"
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": blob_path},
        )
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 200
        body = response.json()
        assert blob_path in body["url"]
        assert body["expires_in"] == settings.signed_url_ttl

    def test_409_for_non_export_job_type(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.SCAN_SOURCE,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": "exports/x/coco.zip"},
        )
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 409

    def test_409_for_a_still_running_export(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(
            sessionmaker, project_id=project.id, type_=JobType.EXPORT, status=JobStatus.RUNNING
        )
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 409

    def test_409_for_a_succeeded_export_with_no_blob_path(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={},
        )
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 409

    def test_409_when_project_has_no_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, result_connector_id=None)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": "exports/x/coco.zip"},
        )
        _login(app)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 409

    def test_409_when_the_result_connector_cannot_sign(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The connector's `signed_url` fails (e.g. its root is gone). Rather than
        rely on `LocalConnector` happening to raise on a missing root (it does
        not — path resolution alone doesn't touch the filesystem), patch
        `storage_for` to simulate any connector that raises `ConnectorError`.
        """
        connector = _seed_local_connector(sessionmaker, root=tmp_path / "nonexistent")
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": "exports/x/coco.zip"},
        )
        _login(app)

        @asynccontextmanager
        async def _broken_storage_for(_connector: Connector) -> AsyncIterator[Any]:
            raise ConnectorError("root does not exist")
            yield  # pragma: no cover - unreachable, keeps this an async generator

        monkeypatch.setattr("app.api.v1.jobs.storage_for", _broken_storage_for)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 409

    def test_403_for_non_member(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, user_id=MEMBER_ID)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": "exports/x/coco.zip"},
        )
        _login(app, user_id=OUTSIDER_ID)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 403

    def test_404_for_another_organizations_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, organization_id=OTHER_ORG_ID)
        job = _seed_job(
            sessionmaker,
            project_id=project.id,
            type_=JobType.EXPORT,
            status=JobStatus.SUCCEEDED,
            result={"blob_path": "exports/x/coco.zip"},
        )
        _login(app, organization_id=ORG_ID)

        response = client.get(f"/api/v1/jobs/{job.id}/download")
        assert response.status_code == 404


class TestListProjectJobs:
    def test_cursor_pagination_round_trip(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        jobs = [
            _seed_job(sessionmaker, project_id=project.id, created_at=base + timedelta(seconds=i))
            for i in range(3)
        ]
        _login(app)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            response = client.get(f"/api/v1/projects/{project.id}/jobs", params=params)
            assert response.status_code == 200
            body = response.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["id"])
            cursor = body["next_cursor"]

        assert seen == [str(jobs[2].id), str(jobs[1].id), str(jobs[0].id)]
        assert cursor is None

    def test_filters_by_status_and_type(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        export_job = _seed_job(
            sessionmaker, project_id=project.id, type_=JobType.EXPORT, status=JobStatus.SUCCEEDED
        )
        _seed_job(
            sessionmaker, project_id=project.id, type_=JobType.SCAN_SOURCE, status=JobStatus.QUEUED
        )
        _login(app)

        response = client.get(
            f"/api/v1/projects/{project.id}/jobs",
            params={"status": "succeeded", "type": "export"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["id"] for item in items] == [str(export_job.id)]

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app, user_id=OUTSIDER_ID)

        response = client.get(f"/api/v1/projects/{project.id}/jobs")
        assert response.status_code == 403


class TestCreateExportJob:
    def test_idempotency_key_replays_the_job(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        queue: FakeJobQueue,
    ) -> None:
        """A retried create with the same `Idempotency-Key` answers the same job (API-2)."""
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)
        headers = {"Idempotency-Key": "export-once"}

        first = client.post(
            f"/api/v1/projects/{project.id}/exports", json={"format": "coco"}, headers=headers
        )
        assert first.status_code == 202, first.text
        second = client.post(
            f"/api/v1/projects/{project.id}/exports", json={"format": "yolo"}, headers=headers
        )
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["payload"]["format"] == "coco"
        assert len(queue.enqueued) == 1

        # The key is per endpoint: the same key on a snapshot is a fresh create.
        other = client.post(
            f"/api/v1/projects/{project.id}/snapshots", json={"name": "s"}, headers=headers
        )
        assert other.status_code == 202, other.text
        assert other.json()["id"] != first.json()["id"]

    def test_lost_idempotency_race_returns_the_winner(
        self, sessionmaker: async_sessionmaker[AsyncSession], queue: FakeJobQueue
    ) -> None:
        """Two concurrent retries: the second commit hits the unique index and replays."""
        from app.services import idempotency
        from app.services.jobs import AuditActor, submit_job

        project = _seed_project(sessionmaker)

        async def _race() -> tuple[uuid.UUID, uuid.UUID]:
            async with sessionmaker() as session:
                winner = Job(
                    project_id=project.id, type=JobType.EXPORT, status=JobStatus.QUEUED, payload={}
                )
                session.add(winner)
                await session.flush()
                idempotency.remember(
                    session,
                    organization_id=ORG_ID,
                    endpoint="job.export",
                    key="raced",
                    target_id=winner.id,
                )
                await session.commit()
                winner_id = winner.id
            async with sessionmaker() as session:
                loser = await submit_job(
                    session,
                    queue,
                    project_id=project.id,
                    job_type=JobType.EXPORT,
                    payload={"format": "coco"},
                    actor=AuditActor(ORG_ID, MEMBER_ID, None),
                    idempotency_key="raced",
                )
                return winner_id, loser.id

        winner_id, returned_id = _run(_race())
        assert returned_id == winner_id
        assert queue.enqueued == []

    def test_queues_an_export_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/exports", json={"format": "coco"})
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "export"
        assert body["status"] == "queued"
        assert body["payload"]["format"] == "coco"

    def test_split_needs_a_snapshot(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)
        url = f"/api/v1/projects/{project.id}/exports"

        response = client.post(url, json={"format": "coco", "split": "train"})
        assert response.status_code == 422

        response = client.post(url, json={"format": "coco", "split": "holdout"})
        assert response.status_code == 422

        snapshot_id = str(uuid.uuid4())
        response = client.post(
            url, json={"format": "coco", "snapshot_id": snapshot_id, "split": "val"}
        )
        assert response.status_code == 202
        assert response.json()["payload"]["split"] == "val"

    def test_unknown_format_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/exports", json={"format": "not-a-real-format"}
        )
        assert response.status_code == 422
        body = response.json()
        assert "not-a-real-format" in body["detail"]
        assert "coco" in body["detail"]

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app, user_id=OUTSIDER_ID)

        response = client.post(f"/api/v1/projects/{project.id}/exports", json={"format": "coco"})
        assert response.status_code == 403

    def test_404_for_another_organizations_project(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app, organization_id=ORG_ID)

        response = client.post(f"/api/v1/projects/{project.id}/exports", json={"format": "coco"})
        assert response.status_code == 404


class TestCreateScanJob:
    def test_queues_a_scan_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/scan", json={})
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "scan_source"
        assert body["status"] == "queued"

        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "scan_source")]


class TestCreateThumbnailJob:
    def test_queues_a_thumbnail_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/thumbnails", json={})
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "thumbnail"
        assert body["status"] == "queued"
        assert body["payload"] == {}
        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "thumbnail")]

    def test_force_and_item_ids_are_carried_through(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)
        item_id = uuid.uuid4()

        response = client.post(
            f"/api/v1/projects/{project.id}/thumbnails",
            json={"force": True, "item_ids": [str(item_id)]},
        )
        assert response.status_code == 202
        assert response.json()["payload"] == {"force": True, "item_ids": [str(item_id)]}

    def test_non_member_is_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/thumbnails", json={})
        assert response.status_code == 403
        assert queue_of(app).enqueued == []


class TestCreateTileJob:
    def test_queues_a_tile_job_with_force_and_item_ids(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        item_id = uuid.uuid4()

        response = client.post(
            f"/api/v1/projects/{project.id}/tiles",
            json={"force": True, "item_ids": [str(item_id)]},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["type"] == "tile_image"
        assert body["payload"] == {"force": True, "item_ids": [str(item_id)]}
        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "tile_image")]

    def test_annotators_and_non_members_are_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)
        response = client.post(f"/api/v1/projects/{project.id}/tiles", json={})
        assert response.status_code == 403
        _seed_membership(sessionmaker, project_id=project.id)
        response = client.post(f"/api/v1/projects/{project.id}/tiles", json={})
        assert response.status_code == 403
        assert queue_of(app).enqueued == []


class TestCreateCacheRebuildJob:
    def test_owner_queues_a_rebuild_with_purge(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/cache/rebuild", json={"purge": True})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["type"] == "rebuild_cache"
        assert body["payload"] == {"purge": True}
        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "rebuild_cache")]

    def test_without_any_cache_connector_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/cache/rebuild", json={})
        assert response.status_code == 409
        assert queue_of(app).enqueued == []

    def test_only_owners_may_rebuild(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.REVIEWER)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/cache/rebuild", json={})
        assert response.status_code == 403
        assert queue_of(app).enqueued == []


class TestCreateExtractTextJob:
    def test_owner_queues_extraction_for_chosen_items_with_force(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)
        item_id = uuid.uuid4()

        response = client.post(
            f"/api/v1/projects/{project.id}/extract-text",
            json={"item_ids": [str(item_id)], "force": True},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["type"] == "extract_text"
        assert body["payload"] == {"force": True, "item_ids": [str(item_id)]}
        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "extract_text")]

    def test_without_a_result_connector_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/extract-text", json={})
        assert response.status_code == 409
        assert queue_of(app).enqueued == []

    def test_only_owners_may_extract(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.ANNOTATOR)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/extract-text", json={})
        assert response.status_code == 403
        assert queue_of(app).enqueued == []


class TestCreatePrelabelJob:
    def test_queues_a_prelabel_job_with_defaults(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        version = _seed_model_version(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(version.id)},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "prelabel"
        assert body["status"] == "queued"
        assert body["payload"]["model_version_id"] == str(version.id)
        assert body["payload"]["filter"] == {"item_status": ["new", "prelabeled"]}
        assert body["payload"]["confidence_threshold"] == 0.0
        assert "limit" not in body["payload"]
        assert "label_schema_version_id" not in body["payload"]

        assert queue_of(app).enqueued == [(uuid.UUID(body["id"]), "prelabel")]

    def test_status_filter_outside_new_or_prelabeled_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Pre-labelling may never target items a human is working on (ML-10)."""
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        version = _seed_model_version(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(version.id), "filter": {"item_status": ["annotating"]}},
        )
        assert response.status_code == 422
        assert queue_of(app).enqueued == []

    def test_full_payload_is_carried_through(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        version = _seed_model_version(sessionmaker)
        schema_version_id = uuid.uuid4()
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={
                "model_version_id": str(version.id),
                "label_schema_version_id": str(schema_version_id),
                "filter": {"item_status": ["new"], "path_prefix": "images/"},
                "limit": 5,
                "confidence_threshold": 0.5,
            },
        )
        assert response.status_code == 202
        payload = response.json()["payload"]
        assert payload["label_schema_version_id"] == str(schema_version_id)
        assert payload["filter"] == {"item_status": ["new"], "path_prefix": "images/"}
        assert payload["limit"] == 5
        assert payload["confidence_threshold"] == 0.5

    def test_404_for_unknown_model_version(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    def test_404_for_a_model_version_of_another_organizations_model(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        version = _seed_model_version(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app, organization_id=ORG_ID)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(version.id)},
        )
        assert response.status_code == 404

    def test_403_for_a_non_owner_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.ANNOTATOR)
        version = _seed_model_version(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(version.id)},
        )
        assert response.status_code == 403

    def test_422_for_a_non_positive_limit(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        version = _seed_model_version(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/prelabel",
            json={"model_version_id": str(version.id), "limit": 0},
        )
        assert response.status_code == 422


def queue_of(app: FastAPI) -> FakeJobQueue:
    """The FakeJobQueue the app fixture installed."""
    override = app.dependency_overrides[get_job_queue]
    return cast(FakeJobQueue, _run(override()))


def _job_status(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> Job:
    async def _load() -> Job:
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            return job

    return _run(_load())


class TestEnqueue:
    def test_export_is_handed_to_the_queue_after_commit(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/exports", json={"format": "yolo"})
        assert response.status_code == 202
        job_id = uuid.UUID(response.json()["id"])

        assert queue_of(app).enqueued == [(job_id, "export")]
        assert _job_status(sessionmaker, job_id).status is JobStatus.QUEUED

    def test_export_filter_is_validated_and_normalised(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/exports",
            json={"format": "coco", "filter": {"item_status": ["approved"]}},
        )
        assert response.status_code == 202
        assert response.json()["payload"]["filter"] == {"item_status": ["approved"]}

        bad = client.post(
            f"/api/v1/projects/{project.id}/exports",
            json={"format": "coco", "filter": {"item_status": ["not-a-status"]}},
        )
        assert bad.status_code == 422

    def test_queue_down_marks_the_job_failed_and_returns_503(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A `queued` row nobody will ever run is worse than an honest failure."""
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)
        queue_of(app).fail_with = QueueUnavailableError("redis is down")

        response = client.post(f"/api/v1/projects/{project.id}/scan", json={})
        assert response.status_code == 503
        assert response.json()["type"].endswith("service-unavailable")

        jobs = client.get(f"/api/v1/projects/{project.id}/jobs").json()["items"]
        assert len(jobs) == 1
        assert jobs[0]["status"] == "failed"
        assert "redis is down" in jobs[0]["error"]


class TestCancelJob:
    def test_cancels_a_queued_job_and_signals_the_worker(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id, status=JobStatus.RUNNING)
        _login(app)

        response = client.post(f"/api/v1/jobs/{job.id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert queue_of(app).aborted == [job.id]

    def test_cancel_still_records_when_the_queue_is_down(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id)
        _login(app)
        queue_of(app).fail_with = QueueUnavailableError("redis is down")

        response = client.post(f"/api/v1/jobs/{job.id}/cancel")
        assert response.status_code == 200
        assert _job_status(sessionmaker, job.id).status is JobStatus.CANCELLED

    @pytest.mark.parametrize(
        "terminal", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
    )
    def test_finished_jobs_cannot_be_cancelled(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        terminal: JobStatus,
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id, status=terminal)
        _login(app)

        response = client.post(f"/api/v1/jobs/{job.id}/cancel")
        assert response.status_code == 409
        assert queue_of(app).aborted == []

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        job = _seed_job(sessionmaker, project_id=project.id)
        _login(app, user_id=OUTSIDER_ID)

        assert client.post(f"/api/v1/jobs/{job.id}/cancel").status_code == 403


class TestRetryJob:
    @pytest.mark.parametrize("previous", [JobStatus.FAILED, JobStatus.CANCELLED])
    def test_requeues_a_failed_or_cancelled_job(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        previous: JobStatus,
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id, status=previous)
        _login(app)

        response = client.post(f"/api/v1/jobs/{job.id}/retry")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["error"] is None
        assert body["progress"] == 0
        assert queue_of(app).enqueued == [(job.id, "export")]
        # Through `requeue`, which clears whatever the queue still holds for this id.
        assert queue_of(app).requeued == [job.id]

    @pytest.mark.parametrize("current", [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED])
    def test_only_finished_unsuccessful_jobs_can_be_retried(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        current: JobStatus,
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        job = _seed_job(sessionmaker, project_id=project.id, status=current)
        _login(app)

        response = client.post(f"/api/v1/jobs/{job.id}/retry")
        assert response.status_code == 409
        assert queue_of(app).enqueued == []


class TestCreateImportJob:
    def test_queues_an_import_job_for_an_owner(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports",
            json={
                "format": "coco",
                "path": "labels/coco.json",
                "class_mapping": {"automobile": "car"},
                "attribute_mapping": {"*": {"truncated": None}},
                "dry_run": True,
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "import"
        assert body["status"] == "queued"
        assert body["payload"] == {
            "format": "coco",
            "path": "labels/coco.json",
            "class_mapping": {"automobile": "car"},
            "attribute_mapping": {"*": {"truncated": None}},
            "status": "submitted",
            "dry_run": True,
            "created_by_id": str(MEMBER_ID),
        }
        assert [kind for _, kind in queue_of(app).enqueued] == ["import"]

    def test_unknown_format_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports", json={"format": "nope", "path": "x.json"}
        )
        assert response.status_code == 422
        assert "voc" in response.json()["detail"]

    def test_403_for_a_non_owner_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports", json={"format": "coco", "path": "x.json"}
        )
        assert response.status_code == 403

    def test_404_for_a_connector_of_another_organization(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        foreign = _seed_local_connector(sessionmaker, root=tmp_path, organization_id=uuid.uuid4())
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports",
            json={"format": "coco", "path": "x.json", "connector_id": str(foreign.id)},
        )
        assert response.status_code == 404


class TestUploadImportJob:
    def test_stores_the_upload_on_the_result_connector_and_queues(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports/upload",
            files={"file": ("coco.json", b'{"images": []}', "application/json")},
            data={
                "format": "coco",
                "status": "draft",
                "class_mapping": '{"a": "car"}',
                "attribute_mapping": '{"car": {"is_occluded": "occluded", "id": null}}',
            },
        )
        assert response.status_code == 202, response.text
        payload = response.json()["payload"]
        assert payload["format"] == "coco"
        assert payload["status"] == "draft"
        assert payload["class_mapping"] == {"a": "car"}
        assert payload["attribute_mapping"] == {"car": {"is_occluded": "occluded", "id": None}}
        assert payload["connector_id"] == str(connector.id)
        assert payload["path"].startswith("imports/") and payload["path"].endswith("/coco.json")
        assert (tmp_path / payload["path"]).read_bytes() == b'{"images": []}'

    def test_413_over_the_size_limit(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.api.v1 import jobs as jobs_module

        monkeypatch.setattr(jobs_module, "MAX_IMPORT_BYTES", 8)
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports/upload",
            files={"file": ("big.json", b"x" * 64, "application/json")},
            data={"format": "coco"},
        )
        assert response.status_code == 413

    def test_409_without_a_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports/upload",
            files={"file": ("coco.json", b"{}", "application/json")},
            data={"format": "coco"},
        )
        assert response.status_code == 409

    def test_422_for_a_class_mapping_that_is_not_an_object(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports/upload",
            files={"file": ("coco.json", b"{}", "application/json")},
            data={"format": "coco", "class_mapping": "[1, 2]"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "raw", ['{"car": ["occluded"]}', '{"car": {"a": 1}}', "not json", "[]"]
    )
    def test_422_for_a_malformed_attribute_mapping_before_storing_anything(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        raw: str,
    ) -> None:
        connector = _seed_local_connector(sessionmaker, root=tmp_path)
        project = _seed_project(sessionmaker, result_connector_id=connector.id)
        _seed_membership(sessionmaker, project_id=project.id, role=ProjectRole.OWNER)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/imports/upload",
            files={"file": ("coco.json", b"{}", "application/json")},
            data={"format": "coco", "attribute_mapping": raw},
        )
        assert response.status_code == 422
        assert not (tmp_path / "imports").exists()
