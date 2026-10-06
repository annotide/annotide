"""Tests for `/projects/{id}/snapshots` (EXP-1).

Same fixture shape as `test_api_jobs.py`: in-memory SQLite with the tables
these endpoints touch, a fake current user, and a `FakeJobQueue`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
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
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationSource,
    AnnotationStatus,
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    IdempotencyKey,
    Item,
    ItemStatus,
    Job,
    Membership,
    Model,
    ModelTask,
    ModelVersion,
    Project,
    ProjectRole,
    Snapshot,
)
from app.services.queue import get_job_queue
from tests.support import FakeJobQueue

ORG_ID = uuid.uuid4()
MEMBER_ID = uuid.uuid4()
OUTSIDER_ID = uuid.uuid4()
SCHEMA_VERSION_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [
        AuditEvent.__table__,
        Connector.__table__,
        IdempotencyKey.__table__,
        Project.__table__,
        Job.__table__,
        Membership.__table__,
        Snapshot.__table__,
        Model.__table__,
        ModelVersion.__table__,
        Item.__table__,
        Annotation.__table__,
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


def _login(app: FastAPI, *, user_id: uuid.UUID = MEMBER_ID) -> None:
    user = CurrentUser(
        id=user_id,
        organization_id=ORG_ID,
        email="member@example.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed_project(
    sessionmaker: async_sessionmaker[AsyncSession], *, member: bool = True
) -> Project:
    async def _create() -> Project:
        async with sessionmaker() as session:
            project = Project(organization_id=ORG_ID, name="proj", settings={}, workflow={})
            session.add(project)
            await session.flush()
            if member:
                session.add(
                    Membership(user_id=MEMBER_ID, project_id=project.id, role=ProjectRole.OWNER)
                )
            await session.commit()
            await session.refresh(project)
            return project

    return _run(_create())


def _seed_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: uuid.UUID, name: str, age_s: int
) -> Snapshot:
    async def _create() -> Snapshot:
        async with sessionmaker() as session:
            snapshot = Snapshot(
                project_id=project_id,
                name=name,
                filter={},
                label_schema_version_id=SCHEMA_VERSION_ID,
                item_count=3,
                blob_path=f"snapshots/{uuid.uuid4()}/",
                digest="ab" * 32,
                created_by_id=MEMBER_ID,
                created_at=datetime.now(UTC) - timedelta(seconds=age_s),
            )
            session.add(snapshot)
            await session.commit()
            await session.refresh(snapshot)
            return snapshot

    return _run(_create())


class TestCreateSnapshot:
    def test_queues_a_snapshot_job_with_the_caller_recorded(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        queue: FakeJobQueue,
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/snapshots",
            json={"name": "release-1", "filter": {"item_status": ["approved"]}},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["type"] == "snapshot"
        assert body["status"] == "queued"
        assert body["payload"] == {
            "name": "release-1",
            "filter": {"item_status": ["approved"]},
            "created_by_id": str(MEMBER_ID),
        }
        assert queue.enqueued == [(uuid.UUID(body["id"]), "snapshot")]

    def test_rejects_an_invalid_filter(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/snapshots",
            json={"name": "x", "filter": {"item_status": ["nope"]}},
        )
        assert response.status_code == 422

    def test_split_is_validated_and_passed_to_the_job(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/snapshots",
            json={"name": "x", "split": {"train": 0.7, "val": 0.2, "test": 0.1, "seed": 3}},
        )
        assert response.status_code == 202
        assert response.json()["payload"]["split"] == {
            "train": 0.7,
            "val": 0.2,
            "test": 0.1,
            "seed": 3,
            "group_by": None,
        }

    def test_rejects_a_split_that_does_not_sum_to_one(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/snapshots",
            json={"name": "x", "split": {"train": 0.5, "val": 0.1, "test": 0.1}},
        )
        assert response.status_code == 422
        assert "sum to 1" in response.text

    def test_403_for_non_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker, member=False)
        _login(app, user_id=OUTSIDER_ID)

        response = client.post(f"/api/v1/projects/{project.id}/snapshots", json={"name": "x"})
        assert response.status_code == 403


class TestListSnapshots:
    def test_lists_newest_first_with_cursor(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        for index in range(3):
            _seed_snapshot(sessionmaker, project.id, f"s{index}", age_s=(3 - index) * 60)
        _login(app)

        first = client.get(f"/api/v1/projects/{project.id}/snapshots?limit=2")
        assert first.status_code == 200
        page = first.json()
        assert [row["name"] for row in page["items"]] == ["s2", "s1"]
        assert page["next_cursor"]

        second = client.get(
            f"/api/v1/projects/{project.id}/snapshots?limit=2&cursor={page['next_cursor']}"
        )
        assert [row["name"] for row in second.json()["items"]] == ["s0"]
        assert second.json()["next_cursor"] is None

    def test_get_one_is_scoped_to_the_project(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        other = _seed_project(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker, other.id, "elsewhere", age_s=0)
        _login(app)

        assert (
            client.get(f"/api/v1/projects/{project.id}/snapshots/{snapshot.id}").status_code == 404
        )
        found = client.get(f"/api/v1/projects/{other.id}/snapshots/{snapshot.id}")
        assert found.status_code == 200
        assert found.json()["digest"] == "ab" * 32


def _seed_result_connector(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: uuid.UUID, root: Path
) -> None:
    async def _create() -> None:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=ORG_ID,
                name="results",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                config={"root": str(root)},
            )
            session.add(connector)
            await session.flush()
            project = await session.get(Project, project_id)
            assert project is not None
            project.result_connector_id = connector.id
            await session.commit()

    _run(_create())


def _write_snapshot_blobs(
    root: Path, snapshot: Snapshot, entries: list[tuple[uuid.UUID, uuid.UUID, int, str, list[str]]]
) -> None:
    """Write a manifest and JSONL for `entries` = (item, annotation, version, path, classes)."""
    folder = root / snapshot.blob_path
    folder.mkdir(parents=True)
    manifest = {
        "item_count": len(entries),
        "digest": snapshot.digest,
        "entries": [
            {"item_id": str(i), "annotation_id": str(a), "version": v, "path": p}
            for i, a, v, p, _ in entries
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    lines = [
        json.dumps(
            {
                "annotation_id": str(a),
                "item_id": str(i),
                "version": v,
                "result": {
                    "schema_version": 1,
                    "media_type": "image",
                    "classification": {},
                    "shapes": [
                        {"id": str(uuid.uuid4()), "type": "bbox", "class": c, "bbox": [1, 2, 3, 4]}
                        for c in classes
                    ],
                },
            }
        )
        for i, a, v, _, classes in entries
    ]
    (folder / "annotations.jsonl").write_text("\n".join(lines) + "\n")


class TestDiffSnapshots:
    """`GET /projects/{id}/snapshots/{base}/diff/{target}` (EXP-4)."""

    def test_diffs_two_snapshots_from_their_blobs(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed_project(sessionmaker)
        _seed_result_connector(sessionmaker, project.id, tmp_path)
        base = _seed_snapshot(sessionmaker, project.id, "v1", age_s=60)
        target = _seed_snapshot(sessionmaker, project.id, "v2", age_s=0)
        kept, changed, gone, new = (uuid.uuid4() for _ in range(4))
        a_kept, a_changed1, a_changed2, a_gone, a_new = (uuid.uuid4() for _ in range(5))
        _write_snapshot_blobs(
            tmp_path,
            base,
            [
                (kept, a_kept, 1, "k.png", ["car"]),
                (changed, a_changed1, 1, "c.png", ["car"]),
                (gone, a_gone, 1, "g.png", ["person"]),
            ],
        )
        _write_snapshot_blobs(
            tmp_path,
            target,
            [
                (kept, a_kept, 1, "k.png", ["car"]),
                (changed, a_changed2, 2, "c.png", ["car", "car"]),
                (new, a_new, 1, "n.png", ["bike"]),
            ],
        )
        _login(app)

        response = client.get(f"/api/v1/projects/{project.id}/snapshots/{base.id}/diff/{target.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["base"]["name"] == "v1"
        assert body["target"]["name"] == "v2"
        assert body["items"] == {
            "added": 1,
            "removed": 1,
            "changed": 1,
            "unchanged": 1,
            "split_moved": 0,
        }
        assert [e["path"] for e in body["added"]] == ["n.png"]
        assert [e["path"] for e in body["removed"]] == ["g.png"]
        assert body["changed"][0]["from_version"] == 1
        assert body["changed"][0]["to_version"] == 2
        assert {c["name"]: c["delta"] for c in body["classes"]} == {
            "car": 1,
            "person": -1,
            "bike": 1,
        }
        assert body["truncated"] is False

    def test_404_when_either_snapshot_is_not_in_the_project(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        other = _seed_project(sessionmaker)
        mine = _seed_snapshot(sessionmaker, project.id, "mine", age_s=0)
        theirs = _seed_snapshot(sessionmaker, other.id, "theirs", age_s=0)
        _login(app)

        response = client.get(f"/api/v1/projects/{project.id}/snapshots/{mine.id}/diff/{theirs.id}")
        assert response.status_code == 404

    def test_409_without_a_result_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        a = _seed_snapshot(sessionmaker, project.id, "a", age_s=1)
        b = _seed_snapshot(sessionmaker, project.id, "b", age_s=0)
        _login(app)

        response = client.get(f"/api/v1/projects/{project.id}/snapshots/{a.id}/diff/{b.id}")
        assert response.status_code == 409


def _seed_version(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    model_name: str,
    version: int,
    snapshot_id: uuid.UUID | None = None,
    snapshot_digest: str | None = None,
    training_run: dict[str, Any] | None = None,
    age_s: int = 0,
) -> ModelVersion:
    async def _create() -> ModelVersion:
        async with sessionmaker() as session:
            model = Model(
                organization_id=ORG_ID,
                name=model_name,
                task=ModelTask.DETECT,
                endpoint_url="http://model.example.com",
                identity_type="none",
            )
            session.add(model)
            await session.flush()
            row = ModelVersion(
                model_id=model.id,
                version=version,
                class_mapping={},
                metrics={},
                snapshot_id=snapshot_id,
                snapshot_digest=snapshot_digest,
                training_run=training_run,
                created_at=datetime.now(UTC) - timedelta(seconds=age_s),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    return _run(_create())


def _seed_model_drafts(
    sessionmaker: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    items: int,
) -> None:
    """`items` items, each with two drafts by the version (counted once)."""

    async def _create() -> None:
        async with sessionmaker() as session:
            connector_id = uuid.uuid4()
            for index in range(items):
                item = Item(
                    project_id=project_id,
                    connector_id=connector_id,
                    path=f"img/{index}.jpg",
                    media_type="image",
                    size_bytes=1,
                    meta={},
                    status=ItemStatus.PRELABELED,
                )
                session.add(item)
                await session.flush()
                for version in (1, 2):
                    session.add(
                        Annotation(
                            item_id=item.id,
                            version=version,
                            label_schema_version_id=SCHEMA_VERSION_ID,
                            author_user_id=None,
                            author_model_version_id=version_id,
                            source=AnnotationSource.MODEL,
                            status=AnnotationStatus.DRAFT,
                            result={"shapes": [], "classification": {}},
                        )
                    )
            await session.commit()

    _run(_create())


class TestSnapshotLineage:
    """`GET /projects/{id}/snapshots/{sid}/lineage` (EXP-8)."""

    def test_lists_versions_linked_by_id_or_digest_with_their_footprint(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker, project.id, "v1", age_s=60)
        other = _seed_project(sessionmaker, member=False)
        by_id = _seed_version(
            sessionmaker,
            model_name="detector",
            version=2,
            snapshot_id=snapshot.id,
            snapshot_digest=snapshot.digest,
            training_run={"id": "run-1"},
            age_s=30,
        )
        by_digest = _seed_version(
            sessionmaker, model_name="segmenter", version=1, snapshot_digest=snapshot.digest
        )
        _seed_version(sessionmaker, model_name="unrelated", version=1, snapshot_digest="cd" * 32)
        _seed_model_drafts(sessionmaker, project.id, by_id.id, items=3)
        _seed_model_drafts(sessionmaker, other.id, by_id.id, items=5)
        _login(app)

        response = client.get(f"/api/v1/projects/{project.id}/snapshots/{snapshot.id}/lineage")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["snapshot"] == {
            "id": str(snapshot.id),
            "name": "v1",
            "digest": snapshot.digest,
            "item_count": 3,
            "created_at": body["snapshot"]["created_at"],
        }
        assert [(v["model_name"], v["version"]) for v in body["versions"]] == [
            ("detector", 2),
            ("segmenter", 1),
        ]
        first, second = body["versions"]
        assert first["id"] == str(by_id.id)
        assert first["training_run"] == {"id": "run-1"}
        assert first["items_predicted"] == 3  # only this project's items, each once
        assert second["id"] == str(by_digest.id)
        assert second["training_run"] is None
        assert second["items_predicted"] == 0

    def test_empty_when_nothing_trained_on_it(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        snapshot = _seed_snapshot(sessionmaker, project.id, "v1", age_s=0)
        _login(app)

        response = client.get(f"/api/v1/projects/{project.id}/snapshots/{snapshot.id}/lineage")

        assert response.status_code == 200
        assert response.json()["versions"] == []

    def test_404_outside_the_project_and_403_for_non_members(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed_project(sessionmaker)
        other = _seed_project(sessionmaker, member=False)
        snapshot = _seed_snapshot(sessionmaker, other.id, "v1", age_s=0)
        _login(app)

        wrong_project = client.get(f"/api/v1/projects/{project.id}/snapshots/{snapshot.id}/lineage")
        assert wrong_project.status_code == 404

        not_member = client.get(f"/api/v1/projects/{other.id}/snapshots/{snapshot.id}/lineage")
        assert not_member.status_code == 403
