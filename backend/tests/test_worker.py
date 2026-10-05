"""Tests for the worker: job bookkeeping, the real job bodies, and the outbox publisher.

Everything runs against in-memory SQLite and a `local` connector rooted in a
temporary directory, so the same code path that writes to Azure Blob in
production is exercised end to end — list, read, write — without a network.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from arq import Retry
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.demo import DEMO_SCHEMA
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
    Job,
    JobStatus,
    JobType,
    LabelSchema,
    LabelSchemaVersion,
    MediaType,
    Model,
    ModelTask,
    ModelVersion,
    OutboxEvent,
    Project,
    Snapshot,
    Task,
    TaskStatus,
    TaskType,
    Webhook,
    WebhookDelivery,
)
from app.schemas import AnnotationResult, LabelSchemaDefinition
from app.services.annotations import create_version
from app.services.models import ModelRejected, ModelUnavailable, Prediction, PredictItem
from app.services.queue import QueueUnavailableError
from app.services.webhooks import seal_secret
from app.worker import jobs, outbox
from app.worker.locks import reap_expired_task_locks
from app.worker.outbox import publish_outbox_events
from tests.support import FakeJobQueue, pdf, png

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [
        AuditEvent.__table__,
        Project.__table__,
        Connector.__table__,
        Item.__table__,
        Annotation.__table__,
        LabelSchema.__table__,
        LabelSchemaVersion.__table__,
        Job.__table__,
        Snapshot.__table__,
        OutboxEvent.__table__,
        Task.__table__,
        Model.__table__,
        ModelVersion.__table__,
        Webhook.__table__,
        WebhookDelivery.__table__,
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
def ctx(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """What arq hands every job function, minus the bits these tests do not use."""
    return {"sessionmaker": sessionmaker, "job_try": 1}


class Fixture:
    """A project wired to one local connector for both source and results."""

    def __init__(
        self, root: Path, project: Project, connector: Connector, schema_version_id: uuid.UUID
    ):
        self.root = root
        self.project = project
        self.connector = connector
        self.schema_version_id = schema_version_id


def _seed_project(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> Fixture:
    async def _create() -> Fixture:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=ORG_ID,
                name="local",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                secret_ref=None,
                config={"root": str(root)},
            )
            session.add(connector)
            await session.flush()
            project = Project(
                organization_id=ORG_ID,
                name="proj",
                source_connector_id=connector.id,
                result_connector_id=connector.id,
                source_prefix="images/",
                settings={},
                workflow={},
            )
            session.add(project)
            await session.flush()
            schema = LabelSchema(project_id=project.id, name="traffic")
            session.add(schema)
            await session.flush()
            version = LabelSchemaVersion(
                label_schema_id=schema.id, version=1, definition=DEMO_SCHEMA
            )
            session.add(version)
            project.label_schema_id = schema.id
            await session.commit()
            return Fixture(root, project, connector, version.id)

    return _run(_create())


def _seed_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID | None,
    job_type: JobType,
    payload: dict[str, Any] | None = None,
    status: JobStatus = JobStatus.QUEUED,
) -> Job:
    async def _create() -> Job:
        async with sessionmaker() as session:
            job = Job(project_id=project_id, type=job_type, status=status, payload=payload or {})
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job

    return _run(_create())


def _mark_started(
    sessionmaker: async_sessionmaker[AsyncSession], project_id: uuid.UUID, *, minutes_ago: int
) -> None:
    """Set `started_at` on the project's running jobs."""

    async def _set() -> None:
        async with sessionmaker() as session:
            rows = await session.scalars(
                select(Job).where(Job.project_id == project_id, Job.status == JobStatus.RUNNING)
            )
            for row in rows:
                row.started_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
            await session.commit()

    _run(_set())


def _set_attempts(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID, attempts: int
) -> None:
    async def _set() -> None:
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.attempts = attempts
            await session.commit()

    _run(_set())


def _load_job(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> Job:
    async def _load() -> Job:
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            return job

    return _run(_load())


def _result(*shapes: dict[str, Any]) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": "image", "classification": {}, "shapes": list(shapes)}
    )


def _bbox(cls: str = "car", coords: tuple[float, ...] = (1, 2, 30, 40)) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "type": "bbox", "class": cls, "bbox": list(coords)}


def _seed_item_with_versions(
    sessionmaker: async_sessionmaker[AsyncSession],
    fx: Fixture,
    path: str,
    *,
    versions: int = 1,
    item_status: ItemStatus = ItemStatus.SUBMITTED,
) -> Item:
    """An item with `versions` annotation versions (each one also writes an outbox row)."""

    async def _create() -> Item:
        async with sessionmaker() as session:
            item = Item(
                project_id=fx.project.id,
                connector_id=fx.connector.id,
                path=path,
                media_type=MediaType.IMAGE,
                size_bytes=10,
                width=640,
                height=480,
                meta={},
                status=item_status,
            )
            session.add(item)
            await session.flush()
            for n in range(versions):
                await create_version(
                    session,
                    item=item,
                    result=_result(_bbox(coords=(1, 2, 30 + n, 40 + n))),
                    label_schema_version_id=fx.schema_version_id,
                    author_user_id=USER_ID,
                    status=AnnotationStatus.SUBMITTED,
                )
            await session.commit()
            await session.refresh(item)
            return item

    return _run(_create())


def _seed_text_item(
    sessionmaker: async_sessionmaker[AsyncSession],
    fx: Fixture,
    path: str,
    shapes: list[dict[str, Any]],
    *,
    size_bytes: int | None = None,
    media_type: MediaType = MediaType.TEXT,
) -> Item:
    """A `text` (or `llm`) item whose latest version carries `shapes`."""
    item = _seed_item_with_versions(sessionmaker, fx, path)

    async def _make_text() -> None:
        async with sessionmaker() as session:
            row = await session.get(Item, item.id)
            assert row is not None
            row.media_type = media_type
            if size_bytes is not None:
                row.size_bytes = size_bytes
            annotation = await session.scalar(
                select(Annotation).where(Annotation.item_id == item.id)
            )
            assert annotation is not None
            annotation.result = {
                "schema_version": 1,
                "media_type": media_type.value,
                "classification": {},
                "shapes": shapes,
            }
            await session.commit()

    _run(_make_text())
    return item


def _seed_model_version(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    class_mapping: dict[str, Any] | None = None,
) -> ModelVersion:
    """A registered model with one version, carrying `class_mapping` (BYOM-2)."""

    async def _create() -> ModelVersion:
        async with sessionmaker() as session:
            model = Model(
                organization_id=ORG_ID,
                name="detector",
                task=ModelTask.DETECT,
                endpoint_url="http://model.example",
                identity_type="none",
                secret_ref=None,
            )
            session.add(model)
            await session.flush()
            version = ModelVersion(
                model_id=model.id, version=1, class_mapping=class_mapping or {}, metrics={}
            )
            session.add(version)
            await session.commit()
            await session.refresh(version)
            return version

    return _run(_create())


class FakeModelClient:
    """Stubs `app.services.models.ModelClient` for the worker's prelabel job.

    `for_model` is an async classmethod (mirroring the real client) that
    always returns the same instance's class, so a test configures behaviour
    once via the class attributes and reads `calls` back afterwards.
    """

    calls: ClassVar[list[tuple[list[PredictItem], LabelSchemaDefinition, float]]] = []
    predictions: ClassVar[list[Prediction]] = []
    raise_error: ClassVar[type[BaseException] | None] = None
    media_types: ClassVar[list[str] | None] = None

    @classmethod
    async def for_model(
        cls,
        endpoint_url: str,
        identity_type: str,
        secret_ref: str | None,
        identity_config: dict[str, Any] | None = None,
    ) -> FakeModelClient:
        return cls()

    async def __aenter__(self) -> FakeModelClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def info(self) -> dict[str, Any]:
        media = type(self).media_types
        return {"name": "fake", **({"media_types": media} if media is not None else {})}

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        *,
        confidence_threshold: float = 0.0,
    ) -> list[Prediction]:
        type(self).calls.append((list(items), schema, confidence_threshold))
        if type(self).raise_error is not None:
            raise type(self).raise_error("boom")
        return type(self).predictions


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeModelClient]:
    """A fresh `FakeModelClient` subclass per test, wired in place of `ModelClient`."""

    client_cls = type(
        "FakeModelClient",
        (FakeModelClient,),
        {"calls": [], "predictions": [], "raise_error": None, "media_types": None},
    )
    monkeypatch.setattr(jobs, "ModelClient", client_cls)
    return client_cls


# --------------------------------------------------------------------------- #
# run_job bookkeeping
# --------------------------------------------------------------------------- #


class TestRunJob:
    def test_progress_is_visible_mid_run_and_throttled(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)
        seen: list[int] = []

        async def progress_now() -> int:
            async with sessionmaker() as session:
                row = await session.get(Job, job.id)
                assert row is not None
                await session.refresh(row)
                return row.progress

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            await report(10)
            seen.append(await progress_now())
            await report(20)  # within the interval: dropped
            seen.append(await progress_now())
            return {}

        _run(jobs.run_job(ctx, str(job.id), work))
        assert seen == [10, 10]
        assert _load_job(sessionmaker, job.id).progress == 100

    def test_a_progress_write_never_lands_on_a_job_that_is_no_longer_running(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)

        async def scenario() -> None:
            async with sessionmaker() as session:
                row = await session.get(Job, job.id)
                assert row is not None
                row.status = JobStatus.CANCELLED
                row.progress = 30
                await session.commit()
            await jobs._progress_reporter(sessionmaker, job.id)(80)

        _run(scenario())
        assert _load_job(sessionmaker, job.id).progress == 30

    def test_success_records_result_progress_and_timestamps(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            await report(50)
            return {"answer": 42}

        result = _run(jobs.run_job(ctx, str(job.id), work))
        assert result["answer"] == 42
        assert "duration_ms" in result

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.SUCCEEDED
        assert stored.progress == 100
        assert stored.attempts == 1
        assert stored.result is not None and stored.result["answer"] == 42
        assert stored.started_at is not None and stored.finished_at is not None

    def test_cancelled_row_is_skipped_without_running(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT, status=JobStatus.CANCELLED)
        ran = False

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            nonlocal ran
            ran = True
            return {}

        result = _run(jobs.run_job(ctx, str(job.id), work))
        assert result == {"skipped": "cancelled"}
        assert ran is False
        assert _load_job(sessionmaker, job.id).status is JobStatus.CANCELLED

    def test_missing_row_is_skipped(self, ctx: dict[str, Any]) -> None:
        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            return {}

        assert _run(jobs.run_job(ctx, str(uuid.uuid4()), work)) == {"skipped": "no such job"}

    def test_transient_error_requeues_with_backoff(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            raise TimeoutError("storage hiccup")

        with pytest.raises(Retry) as excinfo:
            _run(jobs.run_job(ctx, str(job.id), work))
        assert excinfo.value.defer_score == jobs.backoff_seconds(1) * 1000

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.QUEUED
        assert stored.error == "storage hiccup"
        assert stored.attempts == 1

    def test_last_allowed_attempt_fails_for_good(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)
        # The limit counts real runs on the row, not arq's tries (which also
        # count waits for a free project slot): this is the fifth run.
        _set_attempts(sessionmaker, job.id, 4)

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            raise TimeoutError("still broken")

        with pytest.raises(TimeoutError):
            _run(jobs.run_job(ctx, str(job.id), work))

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.FAILED
        assert stored.error is not None and "after 5 attempts" in stored.error

    def test_permanent_error_fails_immediately(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            raise LookupError("no such project")

        with pytest.raises(LookupError):
            _run(jobs.run_job(ctx, str(job.id), work))

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.FAILED
        assert stored.error == "LookupError: no such project"

    def test_a_user_cancellation_mid_run_stays_cancelled(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            # What POST /jobs/{id}/cancel does before arq aborts the task.
            async with sessionmaker() as other:
                live = await other.get(Job, row.id)
                assert live is not None
                live.status = JobStatus.CANCELLED
                await other.commit()
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            _run(jobs.run_job(ctx, str(job.id), work))
        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.CANCELLED
        assert stored.finished_at is not None

    def test_a_worker_shutdown_mid_run_requeues_the_job_for_arqs_rerun(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        job = _seed_job(sessionmaker, None, JobType.EXPORT)
        runs: list[int] = []

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            runs.append(1)
            if len(runs) == 1:
                raise asyncio.CancelledError()  # SIGTERM: arq cancels the task
            return {}

        with pytest.raises(asyncio.CancelledError):
            _run(jobs.run_job(ctx, str(job.id), work))
        assert _load_job(sessionmaker, job.id).status is JobStatus.QUEUED

        _run(jobs.run_job({**ctx, "job_try": 2}, str(job.id), work))
        stored = _load_job(sessionmaker, job.id)
        assert len(runs) == 2, "the re-run is not skipped"
        assert stored.status is JobStatus.SUCCEEDED

    def test_a_project_at_its_job_limit_waits_without_spending_an_attempt(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        ctx["settings"] = SimpleNamespace(job_max_tries=5, job_max_running_per_project=2)
        for _ in range(2):
            _seed_job(sessionmaker, fx.project.id, JobType.EXPORT, status=JobStatus.RUNNING)
        _mark_started(sessionmaker, fx.project.id, minutes_ago=1)
        waiting = _seed_job(sessionmaker, fx.project.id, JobType.EXPORT)
        other_project = _seed_job(sessionmaker, None, JobType.EXPORT)
        ran: list[uuid.UUID] = []

        async def work(session: AsyncSession, row: Job, report: Any) -> dict[str, Any]:
            ran.append(row.id)
            return {}

        with pytest.raises(Retry) as excinfo:
            _run(jobs.run_job(ctx, str(waiting.id), work))
        assert excinfo.value.defer_score == jobs.CAPACITY_RETRY_SECONDS * 1000
        stored = _load_job(sessionmaker, waiting.id)
        assert (stored.status, stored.attempts) == (JobStatus.QUEUED, 0)

        _run(jobs.run_job(ctx, str(other_project.id), work))  # not limited
        assert ran == [other_project.id]

        # A running row older than the job timeout is a dead worker's: a slot.
        _mark_started(sessionmaker, fx.project.id, minutes_ago=120)
        _run(jobs.run_job(ctx, str(waiting.id), work))
        assert ran == [other_project.id, waiting.id]
        assert _load_job(sessionmaker, waiting.id).attempts == 1

    def test_backoff_grows_and_caps(self) -> None:
        assert [jobs.backoff_seconds(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
        assert jobs.backoff_seconds(20) == 300


# --------------------------------------------------------------------------- #
# scan_source
# --------------------------------------------------------------------------- #


class TestScanSource:
    def test_scans_the_project_source_and_creates_items(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        (tmp_path / "images" / "notes.txt").write_text("hello")
        (tmp_path / "images" / "ignored.xyz").write_text("?")
        (tmp_path / "elsewhere.png").write_bytes(png(1, 1, bytearray(3)))

        job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(job.id)))

        assert result["items_scanned"] == 3  # only under the project's source_prefix
        assert result["items_created"] == 2
        assert result["items_skipped"] == 1

        async def _items() -> list[Item]:
            async with sessionmaker() as session:
                return list(await session.scalars(select(Item).order_by(Item.path)))

        items = _run(_items())
        assert [item.path for item in items] == ["images/a.png", "images/notes.txt"]
        assert (items[0].width, items[0].height) == (4, 3)
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED

    def test_opens_one_annotate_task_per_new_item(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        (tmp_path / "images" / "b.png").write_bytes(png(1, 1, bytearray(3)))

        job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(job.id)))

        assert result["items_created"] == 2
        assert result["tasks_opened"] == 2

        async def _tasks() -> list[Task]:
            async with sessionmaker() as session:
                return list(await session.scalars(select(Task)))

        tasks = _run(_tasks())
        assert len(tasks) == 2
        item_ids = {t.item_id for t in tasks}
        assert len(item_ids) == 2  # one per item, each with a real item_id
        for task in tasks:
            assert task.type is TaskType.ANNOTATE
            assert task.status is TaskStatus.OPEN

    def test_rescan_opens_no_additional_tasks(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(1, 1, bytearray(3)))
        (tmp_path / "images" / "b.png").write_bytes(png(1, 1, bytearray((10, 20, 30))))

        first_job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        first_result = _run(jobs.scan_source(ctx, str(first_job.id)))
        assert first_result["tasks_opened"] == 2

        second_job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        second_result = _run(jobs.scan_source(ctx, str(second_job.id)))
        assert second_result["tasks_opened"] == 0

        async def _task_count() -> int:
            async with sessionmaker() as session:
                return len(list(await session.scalars(select(Task))))

        assert _run(_task_count()) == 2

    def test_changed_etag_on_an_existing_item_opens_no_task(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        image_path = tmp_path / "images" / "a.png"
        image_path.write_bytes(png(1, 1, bytearray(3)))

        first_job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        first_result = _run(jobs.scan_source(ctx, str(first_job.id)))
        assert first_result["items_created"] == 1
        assert first_result["tasks_opened"] == 1

        # Overwrite with different content: same path, a new etag, an update
        # rather than a create — SRC-4 territory, not WF-2's.
        image_path.write_bytes(png(1, 1, bytearray((99, 100, 101))))

        second_job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        second_result = _run(jobs.scan_source(ctx, str(second_job.id)))
        assert second_result["items_created"] == 0
        assert second_result["items_updated"] == 1
        assert second_result["tasks_opened"] == 0

        async def _task_count() -> int:
            async with sessionmaker() as session:
                return len(list(await session.scalars(select(Task))))

        assert _run(_task_count()) == 1

    def test_payload_overrides_prefix_and_glob(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "x.png").write_bytes(png(1, 1, bytearray(3)))
        (tmp_path / "other" / "y.txt").write_text("no")

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.SCAN_SOURCE, {"prefix": "other/", "glob": "*.png"}
        )
        result = _run(jobs.scan_source(ctx, str(job.id)))
        assert result["items_created"] == 1

    def test_rescan_is_idempotent(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(1, 1, bytearray(3)))

        first = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        _run(jobs.scan_source(ctx, str(first.id)))
        second = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(second.id)))
        assert (result["items_created"], result["items_updated"]) == (0, 0)

    def test_paths_payload_registers_only_the_named_objects(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        # A storage event (SRC-3) names what arrived; the rest of the source,
        # new or not, waits for a full scan.
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        (tmp_path / "images" / "a.png.bak").write_bytes(b"not the object")
        (tmp_path / "images" / "unannounced.png").write_bytes(png(1, 1, bytearray(3)))

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.SCAN_SOURCE,
            {"paths": ["images/a.png", "images/gone.png", "images/a.png"], "trigger": "event"},
        )
        result = _run(jobs.scan_source(ctx, str(job.id)))

        assert result["items_scanned"] == 1
        assert result["items_created"] == 1
        assert result["tasks_opened"] == 1
        assert result["items_skipped"] == 1
        assert result["errors"] == ["images/gone.png: not in the source"]

        async def _items() -> list[Item]:
            async with sessionmaker() as session:
                return list(await session.scalars(select(Item)))

        items = _run(_items())
        assert [item.path for item in items] == ["images/a.png"]
        assert (items[0].width, items[0].height) == (4, 3)

    def test_a_repeated_event_leaves_an_unchanged_item_alone(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        image = tmp_path / "images" / "a.png"
        image.write_bytes(png(1, 1, bytearray(3)))
        payload = {"paths": ["images/a.png"], "trigger": "event"}

        first = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE, payload)
        assert _run(jobs.scan_source(ctx, str(first.id)))["items_created"] == 1
        # Delivered twice (at-least-once): nothing changes.
        again = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE, payload)
        result = _run(jobs.scan_source(ctx, str(again.id)))
        assert (result["items_created"], result["items_updated"]) == (0, 0)

        # Overwritten: the next event refreshes it (SRC-4), without a new task.
        image.write_bytes(png(1, 1, bytearray((1, 2, 3))))
        later = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE, payload)
        result = _run(jobs.scan_source(ctx, str(later.id)))
        assert (result["items_created"], result["items_updated"]) == (0, 1)
        assert result["tasks_opened"] == 0

    def test_project_without_source_connector_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)

        async def _unset() -> None:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                project.source_connector_id = None
                await session.commit()

        _run(_unset())
        job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        with pytest.raises(LookupError):
            _run(jobs.scan_source(ctx, str(job.id)))
        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.FAILED
        assert stored.error is not None and "source connector" in stored.error


# --------------------------------------------------------------------------- #
# snapshot and export
# --------------------------------------------------------------------------- #


class TestSnapshot:
    def test_freezes_latest_versions_and_writes_manifest_with_digest(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/b.png", versions=2)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=1)
        _seed_item_with_versions(
            sessionmaker, fx, "images/c.png", versions=1, item_status=ItemStatus.APPROVED
        )

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.SNAPSHOT,
            {"name": "v1", "filter": {}, "created_by_id": str(USER_ID)},
        )
        result = _run(jobs.snapshot(ctx, str(job.id)))

        assert result["item_count"] == 3
        prefix = tmp_path / "snapshots" / result["snapshot_id"]
        manifest = json.loads((prefix / "manifest.json").read_text())
        lines = (prefix / "annotations.jsonl").read_text().splitlines()

        assert manifest["digest"] == result["digest"]
        assert manifest["item_count"] == 3
        assert [entry["path"] for entry in manifest["entries"]] == [
            "images/a.png",
            "images/b.png",
            "images/c.png",
        ]
        # The item with two versions is frozen at its latest.
        assert manifest["entries"][1]["version"] == 2
        assert len(lines) == 3
        assert json.loads(lines[1])["version"] == 2

        async def _row() -> Snapshot | None:
            async with sessionmaker() as session:
                return await session.get(Snapshot, uuid.UUID(result["snapshot_id"]))

        row = _run(_row())
        assert row is not None
        assert (row.item_count, row.digest, row.name) == (3, result["digest"], "v1")
        assert row.label_schema_version_id == fx.schema_version_id
        assert row.blob_path == f"snapshots/{result['snapshot_id']}/"

    def test_filter_narrows_the_frozen_set(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        _seed_item_with_versions(sessionmaker, fx, "images/c.png", item_status=ItemStatus.APPROVED)

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.SNAPSHOT,
            {
                "name": "approved-only",
                "filter": {"item_status": ["approved"]},
                "created_by_id": str(USER_ID),
            },
        )
        result = _run(jobs.snapshot(ctx, str(job.id)))
        assert result["item_count"] == 1

    def test_same_content_gives_same_digest(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        payload = {"name": "n", "filter": {}, "created_by_id": str(USER_ID)}

        first = _run(
            jobs.snapshot(
                ctx, str(_seed_job(sessionmaker, fx.project.id, JobType.SNAPSHOT, payload).id)
            )
        )
        second = _run(
            jobs.snapshot(
                ctx, str(_seed_job(sessionmaker, fx.project.id, JobType.SNAPSHOT, payload).id)
            )
        )
        assert first["digest"] == second["digest"]
        assert first["snapshot_id"] != second["snapshot_id"]


class TestWebhookEvents:
    """Worker-side events reach the delivery table (API-4)."""

    def test_snapshot_job_queues_snapshot_created_and_job_succeeded(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")

        async def _hook() -> None:
            async with sessionmaker() as session:
                session.add(
                    Webhook(
                        organization_id=ORG_ID,
                        project_id=fx.project.id,
                        url="https://hooks.example/in",
                        events=["snapshot.created", "job.succeeded"],
                        secret=seal_secret("s" * 64),
                    )
                )
                await session.commit()

        _run(_hook())
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.SNAPSHOT,
            {"name": "v1", "filter": {}, "created_by_id": str(USER_ID)},
        )
        result = _run(jobs.snapshot(ctx, str(job.id)))

        async def _deliveries() -> list[WebhookDelivery]:
            async with sessionmaker() as session:
                return list(
                    await session.scalars(
                        select(WebhookDelivery).order_by(WebhookDelivery.created_at)
                    )
                )

        deliveries = _run(_deliveries())
        assert sorted(d.event for d in deliveries) == ["job.succeeded", "snapshot.created"]
        by_event = {d.event: d.payload for d in deliveries}
        created = by_event["snapshot.created"]["data"]
        assert isinstance(created, dict)
        assert created["snapshot_id"] == result["snapshot_id"]
        assert created["digest"] == result["digest"]
        succeeded = by_event["job.succeeded"]["data"]
        assert isinstance(succeeded, dict)
        assert succeeded["job_id"] == str(job.id)
        assert succeeded["type"] == "snapshot"

    def test_failed_job_queues_job_failed(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)

        async def _hook() -> None:
            async with sessionmaker() as session:
                session.add(
                    Webhook(
                        organization_id=ORG_ID,
                        project_id=None,
                        url="https://hooks.example/in",
                        events=["*"],
                        secret=seal_secret("s" * 64),
                    )
                )
                await session.commit()

        _run(_hook())
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": str(uuid.uuid4())},
        )
        with pytest.raises(LookupError):
            _run(jobs.export(ctx, str(job.id)))

        async def _events() -> list[str]:
            async with sessionmaker() as session:
                return [d.event for d in await session.scalars(select(WebhookDelivery))]

        assert _run(_events()) == ["job.failed"]


class TestSnapshotSplit:
    """Train / val / test partition frozen with the snapshot (EXP-3)."""

    def _split_snapshot(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture
    ) -> dict[str, Any]:
        for folder in ("clip1", "clip2", "clip3", "clip4"):
            for n in range(3):
                _seed_item_with_versions(sessionmaker, fx, f"videos/{folder}/f{n}.png")
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.SNAPSHOT,
            {
                "name": "split",
                "filter": {},
                "split": {"train": 0.5, "val": 0.25, "test": 0.25, "seed": 4, "group_by": "folder"},
                "created_by_id": str(USER_ID),
            },
        )
        return _run(jobs.snapshot(ctx, str(job.id)))

    def test_snapshot_records_the_split_per_entry_and_on_the_row(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        result = self._split_snapshot(ctx, sessionmaker, fx)

        assert result["item_count"] == 12
        assert sum(result["split_counts"].values()) == 12
        manifest = json.loads(
            (tmp_path / "snapshots" / result["snapshot_id"] / "manifest.json").read_text()
        )
        assert manifest["split"]["config"]["group_by"] == "folder"
        # Group-aware: every frame of a clip is in the same split.
        by_folder: dict[str, set[str]] = {}
        for entry in manifest["entries"]:
            by_folder.setdefault(entry["path"].split("/")[1], set()).add(entry["split"])
        assert all(len(names) == 1 for names in by_folder.values())

        async def _row() -> Snapshot | None:
            async with sessionmaker() as session:
                return await session.get(Snapshot, uuid.UUID(result["snapshot_id"]))

        row = _run(_row())
        assert row is not None
        assert row.split == manifest["split"]["config"]

    def test_export_of_a_split_snapshot_has_one_directory_per_split(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        snap = self._split_snapshot(ctx, sessionmaker, fx)

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": snap["snapshot_id"]},
        )
        result = _run(jobs.export(ctx, str(job.id)))

        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        names = archive.namelist()
        present = {name.split("/")[0] for name in names if "/" in name}
        expected = {k for k, v in snap["split_counts"].items() if v > 0}
        assert present == expected
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["split_counts"] == snap["split_counts"]

    def test_export_can_pick_one_split(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        snap = self._split_snapshot(ctx, sessionmaker, fx)
        chosen = next(k for k, v in snap["split_counts"].items() if v > 0)

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": snap["snapshot_id"], "split": chosen},
        )
        result = _run(jobs.export(ctx, str(job.id)))

        assert result["split"] == chosen
        assert result["item_count"] == snap["split_counts"][chosen]
        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        assert {n.split("/")[0] for n in archive.namelist() if "/" in n} == {chosen}

    def test_an_empty_split_snapshot_exports_an_empty_partition(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        # Nothing submitted yet: the snapshot is split but has no entries, so
        # its id -> split map is empty. It is still a split snapshot.
        fx = _seed_project(sessionmaker, tmp_path)
        snap = _run(
            jobs.snapshot(
                ctx,
                str(
                    _seed_job(
                        sessionmaker,
                        fx.project.id,
                        JobType.SNAPSHOT,
                        {
                            "name": "empty",
                            "filter": {},
                            "split": {"train": 0.8, "val": 0.1, "test": 0.1},
                            "created_by_id": str(USER_ID),
                        },
                    ).id
                ),
            )
        )
        assert snap["item_count"] == 0

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "coco", "snapshot_id": snap["snapshot_id"], "split": "train"},
        )
        result = _run(jobs.export(ctx, str(job.id)))

        assert (result["split"], result["item_count"]) == ("train", 0)
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED
        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["split_counts"] == {"train": 0, "val": 0, "test": 0}

    def test_split_export_without_a_split_snapshot_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        snap = _run(
            jobs.snapshot(
                ctx,
                str(
                    _seed_job(
                        sessionmaker,
                        fx.project.id,
                        JobType.SNAPSHOT,
                        {"name": "plain", "filter": {}, "created_by_id": str(USER_ID)},
                    ).id
                ),
            )
        )
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": snap["snapshot_id"], "split": "train"},
        )

        with pytest.raises(LookupError):
            _run(jobs.export(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status is JobStatus.FAILED


class TestExport:
    def test_exports_latest_versions_as_a_zip_on_the_result_connector(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=2)

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.EXPORT, {"format": "coco", "filter": {}}
        )
        result = _run(jobs.export(ctx, str(job.id)))

        assert result["blob_path"] == f"exports/{job.id}/coco.zip"
        assert result["item_count"] == 1
        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert any(name.endswith(".json") and name != "manifest.json" for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["job_id"] == str(job.id)
        assert manifest["format"] == "coco"

    def test_image_only_format_lists_skipped_text_items_in_the_job_result(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        text_item = _seed_item_with_versions(sessionmaker, fx, "docs/b.txt")

        async def _make_text() -> None:
            async with sessionmaker() as session:
                item = await session.get(Item, text_item.id)
                assert item is not None
                item.media_type = MediaType.TEXT
                annotation = await session.scalar(
                    select(Annotation).where(Annotation.item_id == text_item.id)
                )
                assert annotation is not None
                annotation.result = {
                    "schema_version": 1,
                    "media_type": "text",
                    "classification": {},
                    "shapes": [],
                }
                await session.commit()

        _run(_make_text())
        coco = _run(
            jobs.export(
                ctx,
                str(
                    _seed_job(
                        sessionmaker,
                        fx.project.id,
                        JobType.EXPORT,
                        {"format": "coco", "filter": {}},
                    ).id
                ),
            )
        )
        assert len(coco["warnings"]) == 1
        assert coco["warnings"][0].startswith("docs/b.txt: skipped text item")
        native = _run(
            jobs.export(
                ctx,
                str(
                    _seed_job(
                        sessionmaker,
                        fx.project.id,
                        JobType.EXPORT,
                        {"format": "native", "filter": {}},
                    ).id
                ),
            )
        )
        assert native["warnings"] == []

    def test_export_from_a_snapshot_uses_the_frozen_versions(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=1)
        snap = _run(
            jobs.snapshot(
                ctx,
                str(
                    _seed_job(
                        sessionmaker,
                        fx.project.id,
                        JobType.SNAPSHOT,
                        {"name": "frozen", "filter": {}, "created_by_id": str(USER_ID)},
                    ).id
                ),
            )
        )

        # A new version after the snapshot must not leak into the export.
        async def _new_version() -> None:
            async with sessionmaker() as session:
                row = await session.get(Item, item.id)
                assert row is not None
                await create_version(
                    session,
                    item=row,
                    result=_result(_bbox(coords=(5, 5, 99, 99))),
                    label_schema_version_id=fx.schema_version_id,
                    author_user_id=USER_ID,
                )
                await session.commit()

        _run(_new_version())

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": snap["snapshot_id"]},
        )
        result = _run(jobs.export(ctx, str(job.id)))
        assert result["snapshot_id"] == snap["snapshot_id"]
        assert result["digest"] == snap["digest"]

        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        records = [
            json.loads(line) for line in archive.read("annotations.jsonl").decode().splitlines()
        ]
        assert len(records) == 1
        assert records[0]["shapes"][0]["bbox"] == [1, 2, 30, 40]

    def test_unknown_snapshot_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.EXPORT,
            {"format": "native", "snapshot_id": str(uuid.uuid4())},
        )
        with pytest.raises(LookupError):
            _run(jobs.export(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status is JobStatus.FAILED

    def test_spacy_and_conll_read_source_text_and_warn_about_skips(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.txt").write_text("Anna went to Paris", encoding="utf-8")
        text_item = _seed_text_item(
            sessionmaker,
            fx,
            "docs/a.txt",
            [
                {"id": str(uuid.uuid4()), "type": "span", "start": 0, "end": 4, "class": "PER"},
                {"id": str(uuid.uuid4()), "type": "span", "start": 0, "end": 3, "class": "SHORT"},
                {"id": str(uuid.uuid4()), "type": "span", "start": 14, "end": 19, "class": "LOC"},
            ],
        )
        _seed_text_item(sessionmaker, fx, "docs/big.txt", [], size_bytes=17 * 1024 * 1024)

        spacy_job = _seed_job(
            sessionmaker, fx.project.id, JobType.EXPORT, {"format": "spacy", "filter": {}}
        )
        spacy_result = _run(jobs.export(ctx, str(spacy_job.id)))

        assert spacy_result["item_count"] == 3
        warnings = spacy_result["warnings"]
        assert any(w.startswith("images/a.png: skipped image item") for w in warnings)
        assert any(w == "docs/big.txt: skipped, source text could not be read" for w in warnings)
        assert any("dropped for overlapping" in w for w in warnings)

        archive = zipfile.ZipFile(io.BytesIO((tmp_path / spacy_result["blob_path"]).read_bytes()))
        names = set(archive.namelist())
        assert {"annotations.jsonl", "warnings.json", "manifest.json"} <= names
        record = json.loads(archive.read("annotations.jsonl").decode().strip())
        assert record["text"] == "Anna went to Paris"
        assert record["entities"] == [[0, 4, "PER"], [14, 19, "LOC"]]
        assert record["meta"] == {"item_id": str(text_item.id), "path": "docs/a.txt"}

        conll_job = _seed_job(
            sessionmaker, fx.project.id, JobType.EXPORT, {"format": "conll", "filter": {}}
        )
        conll_result = _run(jobs.export(ctx, str(conll_job.id)))
        assert conll_result["item_count"] == 3
        conll_archive = zipfile.ZipFile(
            io.BytesIO((tmp_path / conll_result["blob_path"]).read_bytes())
        )
        conll_text = conll_archive.read("annotations.conll").decode()
        assert f"# item_id = {text_item.id}" in conll_text
        assert "# path = docs/a.txt" in conll_text
        assert "Anna\tB-PER" in conll_text

    def test_llm_export_reads_documents_and_derives_preference_pairs(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """§5 LLM-data: the worker reads each `llm` document for the `llm` format."""
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        (tmp_path / "evals").mkdir()
        document = {
            "messages": [{"role": "user", "content": "2+2?"}],
            "responses": [{"id": "a", "content": "4"}, {"id": "b", "content": "5"}],
        }
        (tmp_path / "evals" / "q1.llm.json").write_text(json.dumps(document), encoding="utf-8")
        item = _seed_text_item(
            sessionmaker,
            fx,
            "evals/q1.llm.json",
            [{"id": str(uuid.uuid4()), "type": "ranking", "class": "car", "order": ["a", "b"]}],
            media_type=MediaType.LLM,
        )

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.EXPORT, {"format": "llm", "filter": {}}
        )
        result = _run(jobs.export(ctx, str(job.id)))

        assert result["warnings"] == [
            "images/a.png: skipped image item, not supported by this format"
        ]
        archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
        record = json.loads(archive.read("annotations.jsonl").decode().strip())
        assert record["item_id"] == str(item.id)
        assert record["responses"] == document["responses"]
        assert [(p["chosen"]["id"], p["rejected"]["id"]) for p in record["preference_pairs"]] == [
            ("a", "b")
        ]


def _tiff(path: Path, width: int, height: int) -> int:
    """Write a deflate-compressed test TIFF with some structure; returns its size."""
    import pyvips

    image = pyvips.Image.black(width, height).draw_rect(
        255, width // 4, height // 4, 50, 40, fill=True
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.write_to_file(f"{path}[compression=deflate]")
    return path.stat().st_size


def _tile_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    base = get_settings()
    patched = base.model_copy(update=overrides)
    monkeypatch.setattr(jobs, "get_settings", lambda: patched)


class TestTileImage:
    def _item(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fx: Fixture,
        path: str,
        size_bytes: int,
    ) -> Item:
        item = _seed_item(sessionmaker, fx, path, width=None, height=None)

        async def _size() -> None:
            async with sessionmaker() as session:
                row = await session.get(Item, item.id)
                assert row is not None
                row.size_bytes = size_bytes
                await session.commit()

        _run(_size())
        return item

    def test_builds_a_dzi_pyramid_thumbnail_and_meta(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "store"
        work = tmp_path / "work"
        work.mkdir()
        _tile_settings(monkeypatch, tile_min_pixels=1_000_000, tile_work_dir=str(work))
        fx = _seed_project(sessionmaker, root)
        size = _tiff(root / "big" / "slide.tif", 3000, 2000)
        item = self._item(sessionmaker, fx, "big/slide.tif", size)

        job = _seed_job(sessionmaker, fx.project.id, JobType.TILE_IMAGE)
        result = _run(jobs.tile_image(ctx, str(job.id)))

        assert {key: result[key] for key in result if key != "duration_ms"} == {
            "selected": 1,
            "tiled": 1,
            "skipped_small": 0,
            "skipped_too_large": 0,
            "errors": [],
        }
        stored = _load_item(sessionmaker, item.id)
        assert stored.meta["tiles"] == {
            "format": "dzi",
            "path": f"cache/tiles/{item.id}/",
            "tile_size": 256,
            "overlap": 0,
            "suffix": "jpeg",
            "max_level": 12,  # ceil(log2(3000))
            "width": 3000,
            "height": 2000,
        }
        assert (stored.width, stored.height) == (3000, 2000)
        prefix = root / "cache" / "tiles" / str(item.id)
        assert (prefix / "image.dzi").exists()
        assert (prefix / "image_files" / "0" / "0_0.jpeg").exists()
        # Level 12 is full size: ceil(3000/256) x ceil(2000/256) = 12 x 8 tiles.
        assert (prefix / "image_files" / "12" / "11_7.jpeg").exists()
        assert not (prefix / "image_files" / "12" / "12_0.jpeg").exists()
        assert stored.thumbnail_path == f"cache/thumbnails/{item.id}.jpg"
        assert (root / stored.thumbnail_path).read_bytes()[:3] == b"\xff\xd8\xff"
        assert list(work.iterdir()) == []  # staging always cleaned up

        # Tiled items are not selected again unless forced.
        again = _seed_job(sessionmaker, fx.project.id, JobType.TILE_IMAGE)
        assert _run(jobs.tile_image(ctx, str(again.id)))["selected"] == 0

    def test_counts_small_too_large_and_unreadable_sources(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tile_settings(monkeypatch, tile_min_pixels=5_000_000, tile_max_source_bytes=10_000_000)
        fx = _seed_project(sessionmaker, tmp_path)
        small = _tiff(tmp_path / "a" / "small.tif", 1000, 1000)
        self._item(sessionmaker, fx, "a/small.tif", small)
        self._item(sessionmaker, fx, "a/huge.tif", 20_000_000)
        (tmp_path / "a" / "broken.tif").write_bytes(b"not an image at all")
        self._item(sessionmaker, fx, "a/broken.tif", 19)

        job = _seed_job(sessionmaker, fx.project.id, JobType.TILE_IMAGE)
        result = _run(jobs.tile_image(ctx, str(job.id)))

        assert result["selected"] == 3
        assert result["tiled"] == 0
        assert result["skipped_small"] == 1
        assert result["skipped_too_large"] == 1
        assert len(result["errors"]) == 1 and "broken.tif" in result["errors"][0]
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED

    def test_scan_chains_a_tile_job_for_large_images(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Any measured image counts as large here.
        _tile_settings(monkeypatch, tile_min_pixels=10)
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        queue = FakeJobQueue()
        ctx["queue"] = queue

        scan = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(scan.id)))

        types = [queued_type for _, queued_type in queue.enqueued]
        assert "tile_image" in types
        tile_job = _load_job(sessionmaker, uuid.UUID(result["tile_job_id"]))
        assert tile_job.type is JobType.TILE_IMAGE
        assert tile_job.payload == {"after_scan_job_id": str(scan.id)}

    def test_scan_chains_tiling_for_unmeasured_images_but_not_tiled_ones(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        # A TIFF is not measured by the scan, and this one is small on disk.
        _tiff(tmp_path / "images" / "a.tif", 300, 200)
        queue = FakeJobQueue()
        ctx["queue"] = queue

        first = _run(
            jobs.scan_source(
                ctx, str(_seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE).id)
            )
        )
        assert "tile_job_id" in first

        async def _mark_tiled() -> None:
            async with sessionmaker() as session:
                for item in await session.scalars(select(Item)):
                    item.meta = {**item.meta, "tiles": {"format": "dzi"}}
                await session.commit()

        _run(_mark_tiled())
        (tmp_path / "images" / "b.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        second = _run(
            jobs.scan_source(
                ctx, str(_seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE).id)
            )
        )
        assert second["items_created"] == 1
        assert "tile_job_id" not in second


def _voc_xml(
    filename: str, boxes: list[tuple[str, int, int, int, int]], size: bool = True
) -> bytes:
    objects = "".join(
        f"<object><name>{name}</name><bndbox><xmin>{x1}</xmin><ymin>{y1}</ymin>"
        f"<xmax>{x2}</xmax><ymax>{y2}</ymax></bndbox></object>"
        for name, x1, y1, x2, y2 in boxes
    )
    dims = "<size><width>640</width><height>480</height></size>" if size else ""
    return f"<annotation><filename>{filename}</filename>{dims}{objects}</annotation>".encode()


def _seed_item(
    sessionmaker: async_sessionmaker[AsyncSession],
    fx: Fixture,
    path: str,
    *,
    status: ItemStatus = ItemStatus.NEW,
    width: int | None = 640,
    height: int | None = 480,
    media_type: MediaType = MediaType.IMAGE,
) -> Item:
    async def _create() -> Item:
        async with sessionmaker() as session:
            item = Item(
                project_id=fx.project.id,
                connector_id=fx.connector.id,
                path=path,
                media_type=media_type,
                size_bytes=10,
                width=width,
                height=height,
                meta={},
                status=status,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return item

    return _run(_create())


def _versions(
    sessionmaker: async_sessionmaker[AsyncSession], item_id: uuid.UUID
) -> list[Annotation]:
    async def _load() -> list[Annotation]:
        async with sessionmaker() as session:
            rows = await session.scalars(
                select(Annotation).where(Annotation.item_id == item_id).order_by(Annotation.version)
            )
            return list(rows)

    return _run(_load())


def _import_job(
    sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture, **overrides: Any
) -> Job:
    payload: dict[str, Any] = {
        "format": "voc",
        "path": "labels.zip",
        "created_by_id": str(USER_ID),
        **overrides,
    }
    return _seed_job(sessionmaker, fx.project.id, JobType.IMPORT, payload)


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class TestImport:
    def test_writes_submitted_versions_and_advances_items(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        a = _seed_item(sessionmaker, fx, "images/a.png")
        b = _seed_item(sessionmaker, fx, "images/sub/b.png", status=ItemStatus.APPROVED)
        (tmp_path / "labels.zip").write_bytes(
            _zip(
                {
                    "a.xml": _voc_xml("a.png", [("car", 1, 2, 30, 40), ("automobile", 5, 5, 9, 9)]),
                    "b.xml": _voc_xml("sub/b.png", [("pedestrian", 10, 10, 20, 20)]),
                    "c.xml": _voc_xml("c.png", [("car", 1, 1, 2, 2)]),
                    "README.txt": b"ignored",
                }
            )
        )
        job = _import_job(sessionmaker, fx, class_mapping={"automobile": "car"})

        result = _run(jobs.import_annotations(ctx, str(job.id)))

        result.pop("duration_ms")
        assert result == {
            "format": "voc",
            "dry_run": False,
            "parsed": 3,
            "matched": 2,
            "unmatched": 1,
            "imported": 2,
            "errors": 0,
            "dropped_shapes": 0,
            "dropped_attributes": 0,
            "classes": {"automobile": 1, "car": 2, "pedestrian": 1},
            "attributes": {},
            "unmatched_sample": ["c.png"],
            "problems": [],
        }
        [version] = _versions(sessionmaker, a.id)
        assert version.status == AnnotationStatus.SUBMITTED
        assert version.source == AnnotationSource.HUMAN
        assert version.author_user_id == USER_ID
        assert version.label_schema_version_id == fx.schema_version_id
        classes = sorted(shape["class"] for shape in version.result["shapes"])
        assert classes == ["car", "car"]
        assert version.result["shapes"][0]["bbox"] == [1.0, 2.0, 30.0, 40.0]
        assert all(uuid.UUID(shape["id"]) for shape in version.result["shapes"])

        async def _statuses() -> tuple[ItemStatus, ItemStatus]:
            async with sessionmaker() as session:
                first = await session.get(Item, a.id)
                second = await session.get(Item, b.id)
                assert first and second
                return first.status, second.status

        # A `new` item moves to `submitted`; an approved one keeps the reviewer's verdict.
        assert _run(_statuses()) == (ItemStatus.SUBMITTED, ItemStatus.APPROVED)
        assert _load_job(sessionmaker, job.id).status == JobStatus.SUCCEEDED

    def test_attribute_mapping_reaches_the_written_version(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        a = _seed_item(sessionmaker, fx, "images/a.png")
        coco = {
            "images": [{"id": 1, "file_name": "a.png", "width": 640, "height": 480}],
            "categories": [{"id": 1, "name": "automobile"}],
            "annotations": [
                {
                    "id": 1,
                    "image_id": 1,
                    "category_id": 1,
                    "bbox": [1, 2, 10, 10],
                    "attributes": {"is_occluded": True, "track_id": 3},
                }
            ],
        }
        (tmp_path / "coco.json").write_text(json.dumps(coco))
        job = _import_job(
            sessionmaker,
            fx,
            format="coco",
            path="coco.json",
            class_mapping={"automobile": "car"},
            attribute_mapping={"car": {"is_occluded": "occluded"}},
        )

        result = _run(jobs.import_annotations(ctx, str(job.id)))

        assert result["imported"] == 1
        assert result["errors"] == 0
        assert result["dropped_attributes"] == 1  # track_id: the class does not declare it
        assert result["attributes"] == {"automobile": ["is_occluded", "track_id"]}
        [version] = _versions(sessionmaker, a.id)
        assert version.result["shapes"][0]["attributes"] == {"occluded": True}

    def test_dry_run_writes_nothing_but_reports_the_same_counts(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item(sessionmaker, fx, "images/a.png")
        (tmp_path / "a.xml").write_bytes(_voc_xml("a.png", [("car", 1, 2, 30, 40)]))
        job = _import_job(sessionmaker, fx, path="a.xml", dry_run=True)

        result = _run(jobs.import_annotations(ctx, str(job.id)))

        assert result["dry_run"] is True
        assert (result["matched"], result["imported"]) == (1, 1)
        assert _versions(sessionmaker, item.id) == []
        assert _load_job(sessionmaker, job.id).status == JobStatus.SUCCEEDED

    def test_unknown_classes_are_dropped_and_drafts_leave_item_status_alone(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item(sessionmaker, fx, "images/a.png")
        (tmp_path / "labels" / "sub").mkdir(parents=True)
        (tmp_path / "labels" / "sub" / "a.xml").write_bytes(
            _voc_xml("a.png", [("car", 1, 2, 30, 40), ("unicorn", 1, 1, 5, 5)])
        )
        job = _import_job(sessionmaker, fx, path="labels/", status="draft")

        result = _run(jobs.import_annotations(ctx, str(job.id)))

        assert (result["imported"], result["dropped_shapes"]) == (1, 1)
        [version] = _versions(sessionmaker, item.id)
        assert version.status == AnnotationStatus.DRAFT
        assert [shape["class"] for shape in version.result["shapes"]] == ["car"]

        async def _status() -> ItemStatus:
            async with sessionmaker() as session:
                row = await session.get(Item, item.id)
                assert row
                return row.status

        assert _run(_status()) == ItemStatus.NEW

    def test_records_failing_the_schema_rules_count_as_errors(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item(sessionmaker, fx, "images/a.png")
        ok = _seed_item(sessionmaker, fx, "images/b.png")
        # `sign` allows only bbox in DEMO_SCHEMA; a polygon for it fails QA-6.
        polygon = (
            b"<annotation><filename>a.png</filename><object><name>sign</name><polygon>"
            b"<point><x>1</x><y>1</y></point><point><x>5</x><y>1</y></point>"
            b"<point><x>5</x><y>5</y></point></polygon></object></annotation>"
        )
        (tmp_path / "labels.zip").write_bytes(
            _zip({"a.xml": polygon, "b.xml": _voc_xml("b.png", [("car", 1, 2, 3, 4)])})
        )
        job = _import_job(sessionmaker, fx)

        result = _run(jobs.import_annotations(ctx, str(job.id)))

        assert (result["matched"], result["imported"], result["errors"]) == (2, 1, 1)
        assert result["problems"] and "a.png" in result["problems"][0]
        assert len(_versions(sessionmaker, ok.id)) == 1
        assert _load_job(sessionmaker, job.id).status == JobStatus.SUCCEEDED

    def test_malformed_input_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "labels.zip").write_bytes(b"not a zip")
        job = _import_job(sessionmaker, fx)

        with pytest.raises(ValueError, match="zip"):
            _run(jobs.import_annotations(ctx, str(job.id)))
        row = _load_job(sessionmaker, job.id)
        assert row.status == JobStatus.FAILED
        assert row.error and "zip" in row.error

    def test_unknown_format_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        job = _import_job(sessionmaker, fx, format="nope")

        with pytest.raises(KeyError):
            _run(jobs.import_annotations(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status == JobStatus.FAILED

    def test_rerun_adds_another_version(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item(sessionmaker, fx, "images/a.png")
        (tmp_path / "a.xml").write_bytes(_voc_xml("a.png", [("car", 1, 2, 30, 40)]))
        for _ in range(2):
            job = _import_job(sessionmaker, fx, path="a.xml")
            _run(jobs.import_annotations(ctx, str(job.id)))
        assert [v.version for v in _versions(sessionmaker, item.id)] == [1, 2]


class TestPrelabel:
    def test_text_items_go_only_to_models_that_serve_text(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        text_item = _seed_item_with_versions(
            sessionmaker, fx, "docs/b.txt", versions=0, item_status=ItemStatus.NEW
        )

        async def _make_text() -> None:
            async with sessionmaker() as session:
                item = await session.get(Item, text_item.id)
                assert item is not None
                item.media_type = MediaType.TEXT
                await session.commit()

        _run(_make_text())
        version = _seed_model_version(sessionmaker, class_mapping={})

        def _prelabel() -> dict[str, Any]:
            job = _seed_job(
                sessionmaker,
                fx.project.id,
                JobType.PRELABEL,
                {"model_version_id": str(version.id)},
            )
            return _run(jobs.prelabel(ctx, str(job.id)))

        # An image model (no `media_types` in /info) never sees the text item.
        result = _prelabel()
        assert result["skipped_media"] == 1
        [(sent, _, _)] = fake_client.calls
        assert [item.media_type for item in sent] == ["image"]

        fake_client.calls.clear()
        fake_client.media_types = ["image", "text"]
        result = _prelabel()
        assert result["skipped_media"] == 0
        [(sent, _, _)] = fake_client.calls
        assert sorted(item.media_type for item in sent) == ["image", "text"]

    def test_pdf_items_go_to_pdf_models_and_keep_their_page(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        doc = _seed_item(
            sessionmaker, fx, "docs/a.pdf", media_type=MediaType.PDF, width=None, height=None
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.media_types = ["image", "pdf"]
        fake_client.predictions = [
            Prediction(
                item_id=str(doc.id),
                result=AnnotationResult.model_validate(
                    {
                        "schema_version": 1,
                        "media_type": "pdf",
                        "shapes": [
                            {
                                "id": str(uuid.uuid4()),
                                "type": "bbox",
                                "class": "car",
                                "page": 3,
                                "bbox": [10, 20, 110, 40],
                                "text": "42,00",
                            }
                        ],
                    }
                ),
                confidence=0.9,
                error=None,
            )
        ]

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["predicted"] == 1
        [(sent, _, _)] = fake_client.calls
        assert [(item.media_type, item.width, item.height) for item in sent] == [("pdf", 0, 0)]
        [stored] = _versions(sessionmaker, doc.id)
        shape = stored.result["shapes"][0]
        assert (shape["page"], shape["text"], shape["class"]) == (3, "42,00", "car")

    def test_a_soft_deleted_model_fails_the_job_without_calling_it(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker)
        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )

        async def _soft_delete() -> None:
            async with sessionmaker() as session:
                model = await session.get(Model, version.model_id)
                assert model is not None
                model.deleted_at = datetime.now(UTC)
                await session.commit()

        _run(_soft_delete())
        with pytest.raises(LookupError):
            _run(jobs.prelabel(ctx, str(job.id)))

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.FAILED
        assert stored.error == f"LookupError: model {version.model_id} does not exist"
        assert fake_client.calls == []

    def test_predicts_and_transitions_new_items(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item1 = _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        item2 = _seed_item_with_versions(
            sessionmaker, fx, "images/b.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={"vehicle": "car"})
        fake_client.predictions = [
            Prediction(
                item_id=str(item1.id),
                result=_result(_bbox(cls="vehicle")),
                confidence=0.9,
                error=None,
            ),
            Prediction(
                item_id=str(item2.id),
                result=_result(_bbox(cls="vehicle", coords=(5, 5, 50, 50))),
                confidence=0.8,
                error=None,
            ),
        ]

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["selected"] == 2
        assert result["predicted"] == 2
        assert result["empty"] == 0
        assert result["skipped_human"] == 0
        assert result["errors"] == 0
        assert result["dropped_shapes"] == 0
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED

        # The model is posted a schema built from its own class names, never
        # the project's (BYOM-2).
        [(_, posted_schema, _)] = fake_client.calls
        posted_names = {c.name for c in posted_schema.classes}
        assert posted_names == {"vehicle"}

        async def _load() -> tuple[list[Item], list[Annotation]]:
            async with sessionmaker() as session:
                items = list(await session.scalars(select(Item).order_by(Item.path)))
                annotations = list(
                    await session.scalars(select(Annotation).order_by(Annotation.item_id))
                )
                return items, annotations

        items, annotations = _run(_load())
        assert [i.status for i in items] == [ItemStatus.PRELABELED, ItemStatus.PRELABELED]
        assert len(annotations) == 2
        for annotation in annotations:
            assert annotation.status is AnnotationStatus.DRAFT
            assert annotation.source is AnnotationSource.MODEL
            assert annotation.author_model_version_id == version.id
            assert annotation.author_user_id is None
            assert annotation.result["shapes"][0]["class"] == "car"

    def test_prioritize_uncertain_writes_task_priority_and_item_meta(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        """ML-6: the least confident prediction lands first in the queue."""
        fx = _seed_project(sessionmaker, tmp_path)
        sure = _seed_item_with_versions(
            sessionmaker, fx, "images/sure.png", versions=0, item_status=ItemStatus.NEW
        )
        doubtful = _seed_item_with_versions(
            sessionmaker, fx, "images/doubtful.png", versions=0, item_status=ItemStatus.NEW
        )
        empty = _seed_item_with_versions(
            sessionmaker, fx, "images/empty.png", versions=0, item_status=ItemStatus.NEW
        )

        async def _open_tasks() -> None:
            async with sessionmaker() as session:
                for item in (sure, doubtful, empty):
                    session.add(
                        Task(
                            item_id=item.id,
                            project_id=fx.project.id,
                            type=TaskType.ANNOTATE,
                            status=TaskStatus.OPEN,
                        )
                    )
                await session.commit()

        _run(_open_tasks())
        version = _seed_model_version(sessionmaker, class_mapping={"vehicle": "car"})
        fake_client.predictions = [
            Prediction(
                item_id=str(sure.id),
                result=_result({**_bbox(cls="vehicle"), "confidence": 0.95}),
                confidence=0.95,
                error=None,
            ),
            Prediction(
                item_id=str(doubtful.id),
                result=_result(
                    {**_bbox(cls="vehicle"), "confidence": 0.9},
                    {**_bbox(cls="vehicle", coords=(5, 5, 50, 50)), "confidence": 0.4},
                ),
                confidence=0.4,
                error=None,
            ),
            Prediction(item_id=str(empty.id), result=_result(), confidence=None, error=None),
        ]

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.PRELABEL,
            {"model_version_id": str(version.id), "prioritize_uncertain": True},
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["prioritized"] == 3
        assert result["empty"] == 1

        async def _load() -> tuple[dict[str, int], dict[str, float]]:
            async with sessionmaker() as session:
                items = list(await session.scalars(select(Item)))
                tasks = list(await session.scalars(select(Task)))
                path_of = {i.id: i.path for i in items}
                return (
                    {path_of[t.item_id]: t.priority for t in tasks},
                    {i.path: float(i.meta["uncertainty"]) for i in items},
                )

        priorities, scores = _run(_load())
        # 1 - min(confidence): 0.05 → 5, 0.6 → 60; an empty prediction is 1.0 → 100.
        assert priorities == {
            "images/sure.png": 5,
            "images/doubtful.png": 60,
            "images/empty.png": 100,
        }
        assert scores == {
            "images/sure.png": 0.05,
            "images/doubtful.png": 0.6,
            "images/empty.png": 1.0,
        }

    def test_without_prioritize_uncertain_tasks_are_left_alone(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )

        async def _open_task() -> None:
            async with sessionmaker() as session:
                session.add(
                    Task(
                        item_id=item.id,
                        project_id=fx.project.id,
                        type=TaskType.ANNOTATE,
                        status=TaskStatus.OPEN,
                        priority=3,
                    )
                )
                await session.commit()

        _run(_open_task())
        version = _seed_model_version(sessionmaker, class_mapping={"vehicle": "car"})
        fake_client.predictions = [
            Prediction(
                item_id=str(item.id),
                result=_result({**_bbox(cls="vehicle"), "confidence": 0.2}),
                confidence=0.2,
                error=None,
            )
        ]

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert "prioritized" not in result

        async def _load() -> tuple[int, dict[str, object]]:
            async with sessionmaker() as session:
                task = await session.scalar(select(Task))
                row = await session.get(Item, item.id)
                assert task is not None and row is not None
                return task.priority, dict(row.meta)

        priority, meta = _run(_load())
        assert priority == 3
        assert "uncertainty" not in meta

    def test_item_with_human_version_is_never_touched(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item_new = _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        item_human = _seed_item_with_versions(
            sessionmaker, fx, "images/b.png", versions=1, item_status=ItemStatus.PRELABELED
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.predictions = [
            Prediction(
                item_id=str(item_new.id),
                result=_result(_bbox(cls="car")),
                confidence=0.9,
                error=None,
            )
        ]

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["selected"] == 1
        assert result["skipped_human"] == 1
        assert result["predicted"] == 1

        [(sent_items, _, _)] = fake_client.calls
        assert [i.id for i in sent_items] == [str(item_new.id)]

        async def _reload(item_id: uuid.UUID) -> Item:
            async with sessionmaker() as session:
                loaded = await session.get(Item, item_id)
                assert loaded is not None
                return loaded

        human_row = _run(_reload(item_human.id))
        assert human_row.status is ItemStatus.PRELABELED  # untouched

    def test_limit_caps_the_selection(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        _seed_item_with_versions(
            sessionmaker, fx, "images/b.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={})

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.PRELABEL,
            {"model_version_id": str(version.id), "limit": 1},
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["selected"] == 1
        [(sent_items, _, _)] = fake_client.calls
        assert len(sent_items) == 1

    def test_rerun_of_the_same_version_is_idempotent(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        """A retry after a partial commit, or a plain re-run, adds no second draft."""
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.predictions = [
            Prediction(item_id=str(item.id), result=_result(_bbox()), confidence=0.9, error=None)
        ]

        first = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        assert _run(jobs.prelabel(ctx, str(first.id)))["predicted"] == 1

        second = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(second.id)))

        assert result["selected"] == 0
        assert result["skipped_done"] == 1
        assert len(fake_client.calls) == 1

        async def _count() -> int:
            async with sessionmaker() as session:
                rows = await session.scalars(
                    select(Annotation).where(Annotation.item_id == item.id)
                )
                return len(list(rows))

        assert _run(_count()) == 1

    def test_hand_crafted_status_filter_cannot_reach_human_items(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        """`annotating` in the payload is clamped away even though the API forbids it (ML-10)."""
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.ANNOTATING
        )
        version = _seed_model_version(sessionmaker, class_mapping={})

        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.PRELABEL,
            {"model_version_id": str(version.id), "filter": {"item_status": ["annotating"]}},
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["selected"] == 0
        assert fake_client.calls == []

    def test_prediction_error_counts_and_creates_no_version(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.predictions = [
            Prediction(
                item_id=str(item.id), result=_result(), confidence=0.0, error="inference failed"
            )
        ]

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        result = _run(jobs.prelabel(ctx, str(job.id)))

        assert result["errors"] == 1
        assert result["predicted"] == 0

        async def _load() -> tuple[Item, list[Annotation]]:
            async with sessionmaker() as session:
                loaded = await session.get(Item, item.id)
                assert loaded is not None
                annotations = list(await session.scalars(select(Annotation)))
                return loaded, annotations

        row, annotations = _run(_load())
        assert row.status is ItemStatus.NEW  # never transitioned: nothing to show for it
        assert annotations == []

    def test_model_rejected_fails_the_job_permanently(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.raise_error = ModelRejected

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        with pytest.raises(ModelRejected):
            _run(jobs.prelabel(ctx, str(job.id)))

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.FAILED

    def test_model_unavailable_is_retried(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[FakeModelClient],
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(
            sessionmaker, fx, "images/a.png", versions=0, item_status=ItemStatus.NEW
        )
        version = _seed_model_version(sessionmaker, class_mapping={})
        fake_client.raise_error = ModelUnavailable

        job = _seed_job(
            sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
        )
        with pytest.raises(Retry):
            _run(jobs.prelabel(ctx, str(job.id)))

        stored = _load_job(sessionmaker, job.id)
        assert stored.status is JobStatus.QUEUED

    def test_unknown_model_version_fails_permanently(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        job = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.PRELABEL,
            {"model_version_id": str(uuid.uuid4())},
        )
        with pytest.raises(LookupError):
            _run(jobs.prelabel(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status is JobStatus.FAILED


# --------------------------------------------------------------------------- #
# outbox publisher (DATA-2)
# --------------------------------------------------------------------------- #


def _events(sessionmaker: async_sessionmaker[AsyncSession]) -> list[OutboxEvent]:
    async def _load() -> list[OutboxEvent]:
        async with sessionmaker() as session:
            return list(await session.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at)))

    return _run(_load())


class TestOutboxPublisher:
    def test_writes_each_annotation_document_and_stamps_the_event(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=2)
        assert all(event.published_at is None for event in _events(sessionmaker))

        result = _run(publish_outbox_events(ctx))
        assert result == {"published": 2, "failed": 0}

        for version in (1, 2):
            path = tmp_path / "annotations" / str(fx.project.id) / str(item.id) / f"v{version}.json"
            document = json.loads(path.read_text())
            assert document["version"] == version
            assert document["item_path"] == "images/a.png"
            # DATA-8: the JSON key is `class`, never the Python field name.
            assert "class" in document["result"]["shapes"][0]
            assert "class_" not in document["result"]["shapes"][0]

        async def _annotations() -> list[Annotation]:
            async with sessionmaker() as session:
                return list(await session.scalars(select(Annotation).order_by(Annotation.version)))

        for annotation in _run(_annotations()):
            assert annotation.blob_path == (
                f"annotations/{fx.project.id}/{item.id}/v{annotation.version}.json"
            )
        assert all(event.published_at is not None for event in _events(sessionmaker))
        assert all(event.attempts == 1 for event in _events(sessionmaker))

        # Nothing left: the second tick is a no-op.
        assert _run(publish_outbox_events(ctx)) == {"published": 0, "failed": 0}

    def test_failed_events_stay_unpublished_and_count_attempts(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")

        async def _break_result_connector() -> None:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                project.result_connector_id = None
                await session.commit()

        _run(_break_result_connector())

        assert _run(publish_outbox_events(ctx)) == {"published": 0, "failed": 1}
        [event] = _events(sessionmaker)
        assert event.published_at is None
        assert event.attempts == 1

    def test_poison_events_are_left_alone_after_max_attempts(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")

        async def _exhaust() -> None:
            async with sessionmaker() as session:
                for event in await session.scalars(select(OutboxEvent)):
                    event.attempts = 10
                await session.commit()

        _run(_exhaust())
        assert _run(publish_outbox_events(ctx)) == {"published": 0, "failed": 0}

    def test_a_tick_drains_the_backlog_batch_by_batch(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        # One batch per tick capped publishing at a batch a minute (loadtest/).
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=3)
        ctx["settings"] = get_settings().model_copy(update={"outbox_poll_batch_size": 1})

        assert _run(publish_outbox_events(ctx)) == {"published": 3, "failed": 0}
        assert all(event.published_at is not None for event in _events(sessionmaker))

    def test_a_failed_batch_ends_the_tick(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=2)
        ctx["settings"] = get_settings().model_copy(update={"outbox_poll_batch_size": 1})

        async def _break_result_connector() -> None:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                project.result_connector_id = None
                await session.commit()

        _run(_break_result_connector())

        # Storage trouble waits for the next tick instead of using up attempts.
        assert _run(publish_outbox_events(ctx)) == {"published": 0, "failed": 1}
        assert sorted(event.attempts for event in _events(sessionmaker)) == [0, 1]

    def test_a_tick_stops_claiming_once_its_time_is_up(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=2)
        ctx["settings"] = get_settings().model_copy(update={"outbox_poll_batch_size": 1})
        monkeypatch.setattr(outbox, "TICK_BUDGET_SECONDS", 0.0)

        assert _run(publish_outbox_events(ctx)) == {"published": 1, "failed": 0}
        assert _run(publish_outbox_events(ctx)) == {"published": 1, "failed": 0}

    def test_unknown_event_types_are_stamped_not_retried(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async def _seed() -> None:
            async with sessionmaker() as session:
                session.add(
                    OutboxEvent(
                        aggregate_type="mystery",
                        aggregate_id=uuid.uuid4(),
                        type="mystery.happened",
                        payload={},
                    )
                )
                await session.commit()

        _run(_seed())
        assert _run(publish_outbox_events(ctx)) == {"published": 1, "failed": 0}
        [event] = _events(sessionmaker)
        assert event.published_at is not None


# --------------------------------------------------------------------------- #
# dataset selection shared by snapshot and export
# --------------------------------------------------------------------------- #


class TestSelectDataset:
    def test_filters_by_annotation_status_and_path_prefix(
        self, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        from app.services.datasets import DatasetFilter, select_dataset

        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        _seed_item_with_versions(sessionmaker, fx, "other/b.png")

        async def _approve_b() -> None:
            async with sessionmaker() as session:
                item = await session.scalar(select(Item).where(Item.path == "other/b.png"))
                assert item is not None
                annotation = await session.scalar(
                    select(Annotation).where(Annotation.item_id == item.id)
                )
                assert annotation is not None
                annotation.status = AnnotationStatus.APPROVED
                await session.commit()

        _run(_approve_b())

        async def _select(dataset_filter: DatasetFilter) -> list[str]:
            async with sessionmaker() as session:
                entries = await select_dataset(session, fx.project.id, dataset_filter)
                return [entry.item.path for entry in entries]

        assert _run(_select(DatasetFilter())) == ["images/a.png", "other/b.png"]
        assert _run(_select(DatasetFilter(path_prefix="images/"))) == ["images/a.png"]
        assert _run(_select(DatasetFilter(annotation_status=[AnnotationStatus.APPROVED]))) == [
            "other/b.png"
        ]

    def test_filters_by_class_annotator_source_and_date(
        self, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """EXP-2: every extra filter narrows on the item's *latest* version."""
        from app.services.datasets import DatasetFilter, select_dataset

        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png")
        _seed_item_with_versions(sessionmaker, fx, "images/b.png")
        other_user = uuid.uuid4()
        long_ago = datetime(2020, 1, 1, tzinfo=UTC)

        async def _reshape_b() -> None:
            async with sessionmaker() as session:
                item = await session.scalar(select(Item).where(Item.path == "images/b.png"))
                assert item is not None
                annotation = await session.scalar(
                    select(Annotation).where(Annotation.item_id == item.id)
                )
                assert annotation is not None
                annotation.result = _result(_bbox("bus"), _bbox("person")).model_dump(
                    mode="json", by_alias=True
                )
                annotation.author_user_id = other_user
                annotation.source = AnnotationSource.MODEL
                annotation.created_at = long_ago
                await session.commit()

        _run(_reshape_b())

        async def _select(dataset_filter: DatasetFilter) -> list[str]:
            async with sessionmaker() as session:
                entries = await select_dataset(session, fx.project.id, dataset_filter)
                return [entry.item.path for entry in entries]

        assert _run(_select(DatasetFilter(classes=["car"]))) == ["images/a.png"]
        assert _run(_select(DatasetFilter(classes=["person", "tree"]))) == ["images/b.png"]
        assert _run(_select(DatasetFilter(classes=["tree"]))) == []
        assert _run(_select(DatasetFilter(annotator_ids=[other_user]))) == ["images/b.png"]
        assert _run(_select(DatasetFilter(source=[AnnotationSource.HUMAN]))) == ["images/a.png"]
        assert _run(_select(DatasetFilter(annotated_after=long_ago + timedelta(days=1)))) == [
            "images/a.png"
        ]
        assert _run(_select(DatasetFilter(annotated_before=long_ago + timedelta(days=1)))) == [
            "images/b.png"
        ]
        # Filters combine with AND; a naive bound is read as UTC.
        assert _run(
            _select(DatasetFilter(classes=["car", "bus"], annotated_before=datetime(2021, 1, 1)))
        ) == ["images/b.png"]

    def test_items_without_annotations_are_not_part_of_a_dataset(
        self, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        from app.services.datasets import DatasetFilter, select_dataset

        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item_with_versions(sessionmaker, fx, "images/a.png", versions=0)

        async def _count() -> int:
            async with sessionmaker() as session:
                return len(await select_dataset(session, fx.project.id, DatasetFilter()))

        assert _run(_count()) == 0


# --------------------------------------------------------------------------- #
# thumbnail (IMG-8)
# --------------------------------------------------------------------------- #


def _load_item(sessionmaker: async_sessionmaker[AsyncSession], item_id: uuid.UUID) -> Item:
    async def _load() -> Item:
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            return item

    return _run(_load())


class TestThumbnail:
    def test_writes_a_jpeg_per_image_and_records_the_path(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(40, 20, bytearray(40 * 20 * 3)))
        a = _seed_item(sessionmaker, fx, "images/a.png")

        job = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        result = _run(jobs.thumbnail(ctx, str(job.id)))

        assert result["selected"] == 1
        assert result["written"] == 1
        assert result["errors"] == []
        stored = _load_item(sessionmaker, a.id)
        assert stored.thumbnail_path == f"cache/thumbnails/{a.id}.jpg"
        blob = tmp_path / "cache" / "thumbnails" / f"{a.id}.jpg"
        assert blob.read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG SOI marker
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED

    def test_pdf_items_get_a_page_one_thumbnail_and_their_page_metadata(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.pdf").write_bytes(pdf(["Cover", ""], rotate={1: 90}))
        (tmp_path / "docs" / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
        a = _seed_item(sessionmaker, fx, "docs/a.pdf", media_type=MediaType.PDF, width=None)
        _seed_item(sessionmaker, fx, "docs/broken.pdf", media_type=MediaType.PDF, width=None)

        job = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        result = _run(jobs.thumbnail(ctx, str(job.id)))

        assert result["selected"] == 2
        assert result["written"] == 1
        assert [error.split(":")[0] for error in result["errors"]] == ["docs/broken.pdf"]
        stored = _load_item(sessionmaker, a.id)
        assert stored.thumbnail_path == f"cache/thumbnails/{a.id}.jpg"
        assert stored.meta == {
            "page_count": 2,
            "page_sizes": [[600.0, 800.0], [800.0, 600.0]],
            "text_layer": True,
        }
        blob = tmp_path / "cache" / "thumbnails" / f"{a.id}.jpg"
        assert blob.read_bytes()[:3] == b"\xff\xd8\xff"

    def test_skips_items_that_already_have_one_unless_forced(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 4, bytearray(4 * 4 * 3)))
        a = _seed_item(sessionmaker, fx, "images/a.png")

        first = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        assert _run(jobs.thumbnail(ctx, str(first.id)))["written"] == 1
        again = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        assert _run(jobs.thumbnail(ctx, str(again.id)))["selected"] == 0
        forced = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL, {"force": True})
        assert _run(jobs.thumbnail(ctx, str(forced.id)))["written"] == 1
        subset = _seed_job(
            sessionmaker,
            fx.project.id,
            JobType.THUMBNAIL,
            {"force": True, "item_ids": [str(a.id), str(uuid.uuid4())]},
        )
        assert _run(jobs.thumbnail(ctx, str(subset.id)))["selected"] == 1

    def test_counts_undecodable_and_oversized_sources_without_failing(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "ok.png").write_bytes(png(4, 4, bytearray(4 * 4 * 3)))
        (tmp_path / "images" / "bad.png").write_bytes(b"not a png at all")
        (tmp_path / "images" / "huge.png").write_bytes(png(4, 4, bytearray(4 * 4 * 3)))
        ok = _seed_item(sessionmaker, fx, "images/ok.png")
        bad = _seed_item(sessionmaker, fx, "images/bad.png")
        huge = _seed_item(sessionmaker, fx, "images/huge.png")
        _seed_item(sessionmaker, fx, "images/missing.png")

        async def _mark_huge() -> None:
            async with sessionmaker() as session:
                row = await session.get(Item, huge.id)
                assert row is not None
                row.size_bytes = 10**9
                await session.commit()

        _run(_mark_huge())

        job = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        result = _run(jobs.thumbnail(ctx, str(job.id)))

        assert result["selected"] == 4
        assert result["written"] == 1
        assert result["skipped_too_large"] == 1
        assert len(result["errors"]) == 2
        assert any("bad.png" in error for error in result["errors"])
        assert any("missing.png" in error for error in result["errors"])
        assert _load_item(sessionmaker, ok.id).thumbnail_path is not None
        assert _load_item(sessionmaker, bad.id).thumbnail_path is None
        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED

    def test_fails_permanently_without_a_result_connector(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)

        async def _detach() -> None:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                project.result_connector_id = None
                await session.commit()

        _run(_detach())
        job = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)

        with pytest.raises(LookupError):
            _run(jobs.thumbnail(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status is JobStatus.FAILED


class TestScanChainsThumbnails:
    def test_scan_queues_a_thumbnail_job_for_new_images(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(4, 3, bytearray(4 * 3 * 3)))
        queue = FakeJobQueue()
        ctx["queue"] = queue

        scan = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(scan.id)))

        assert result["items_created"] == 1
        assert len(queue.enqueued) == 1
        queued_id, queued_type = queue.enqueued[0]
        assert queued_type == "thumbnail"
        assert result["thumbnail_job_id"] == str(queued_id)
        chained = _load_job(sessionmaker, queued_id)
        assert chained.type is JobType.THUMBNAIL
        assert chained.status is JobStatus.QUEUED
        assert chained.payload == {"after_scan_job_id": str(scan.id)}

    def test_rescan_with_nothing_new_queues_nothing(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(1, 1, bytearray(3)))
        queue = FakeJobQueue()
        ctx["queue"] = queue

        first = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        _run(jobs.scan_source(ctx, str(first.id)))
        second = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(second.id)))

        assert result["items_created"] == 0
        assert "thumbnail_job_id" not in result
        assert len(queue.enqueued) == 1

    def test_no_queue_in_context_is_not_an_error(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(png(1, 1, bytearray(3)))

        scan = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
        result = _run(jobs.scan_source(ctx, str(scan.id)))

        assert result["items_created"] == 1
        assert "thumbnail_job_id" not in result
        assert _load_job(sessionmaker, scan.id).status is JobStatus.SUCCEEDED


class TestTaskLockReaper:
    def test_reopens_expired_in_progress_tasks(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project_id = uuid.uuid4()
        holder = uuid.uuid4()

        async def _seed() -> uuid.UUID:
            async with sessionmaker() as session:
                session.add(
                    Task(
                        item_id=uuid.uuid4(),
                        project_id=project_id,
                        type=TaskType.ANNOTATE,
                        status=TaskStatus.IN_PROGRESS,
                        assignee_id=holder,
                        locked_by_id=holder,
                        locked_until=datetime.now(UTC) - timedelta(minutes=1),
                    )
                )
                live = Task(
                    item_id=uuid.uuid4(),
                    project_id=project_id,
                    type=TaskType.ANNOTATE,
                    status=TaskStatus.IN_PROGRESS,
                    assignee_id=holder,
                    locked_by_id=holder,
                    locked_until=datetime.now(UTC) + timedelta(minutes=30),
                )
                session.add(live)
                await session.commit()
                return live.id

        live_id = _run(_seed())

        assert _run(reap_expired_task_locks(ctx)) == {"reopened": 1}
        assert _run(reap_expired_task_locks(ctx)) == {"reopened": 0}

        async def _load() -> list[Task]:
            async with sessionmaker() as session:
                return list(await session.scalars(select(Task).order_by(Task.created_at)))

        tasks = _run(_load())
        by_status = {t.status for t in tasks}
        assert by_status == {TaskStatus.OPEN, TaskStatus.IN_PROGRESS}
        still_live = next(t for t in tasks if t.id == live_id)
        assert still_live.locked_by_id == holder
        reopened = next(t for t in tasks if t.id != live_id)
        assert reopened.locked_by_id is None
        assert reopened.assignee_id is None


def _seed_local_connector(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> Connector:
    async def _create() -> Connector:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=ORG_ID,
                name=f"local-{uuid.uuid4().hex[:6]}",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                config={"root": str(root)},
            )
            session.add(connector)
            await session.commit()
            await session.refresh(connector)
            return connector

    return _run(_create())


def _set_item(
    sessionmaker: async_sessionmaker[AsyncSession], item_id: uuid.UUID, **values: Any
) -> None:
    async def _update() -> None:
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None
            for key, value in values.items():
                setattr(item, key, value)
            await session.commit()

    _run(_update())


def _use_cache_connector(
    sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture, connector_id: uuid.UUID | None
) -> None:
    async def _update() -> None:
        async with sessionmaker() as session:
            project = await session.get(Project, fx.project.id)
            assert project is not None
            project.cache_connector_id = connector_id
            await session.commit()

    _run(_update())


class TestCacheConnector:
    def test_thumbnails_go_to_the_cache_connector_when_one_is_set(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path / "data")
        (tmp_path / "data" / "images").mkdir(parents=True)
        (tmp_path / "data" / "images" / "a.png").write_bytes(png(8, 8, bytearray(8 * 8 * 3)))
        a = _seed_item(sessionmaker, fx, "images/a.png")
        cache = _seed_local_connector(sessionmaker, tmp_path / "derived")
        _use_cache_connector(sessionmaker, fx, cache.id)

        job = _seed_job(sessionmaker, fx.project.id, JobType.THUMBNAIL)
        assert _run(jobs.thumbnail(ctx, str(job.id)))["written"] == 1

        assert (tmp_path / "derived" / "cache" / "thumbnails" / f"{a.id}.jpg").is_file()
        assert not (tmp_path / "data" / "cache").exists()


class TestRebuildCache:
    def _derived(self, root: Path, item_id: uuid.UUID, *, tiles: bool) -> None:
        (root / "cache" / "thumbnails").mkdir(parents=True, exist_ok=True)
        (root / "cache" / "thumbnails" / f"{item_id}.jpg").write_bytes(b"old")
        if tiles:
            level = root / "cache" / "tiles" / str(item_id) / "image_files" / "0"
            level.mkdir(parents=True)
            (level / "0_0.jpeg").write_bytes(b"old")
            (root / "cache" / "tiles" / str(item_id) / "image.dzi").write_bytes(b"<Image/>")

    def test_purges_clears_and_chains_thumbnail_and_tile_jobs(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        plain = _seed_item(sessionmaker, fx, "images/a.png")
        tiled = _seed_item(sessionmaker, fx, "images/big.tif")
        text = _seed_item(sessionmaker, fx, "notes.txt", media_type=MediaType.TEXT)
        _set_item(sessionmaker, plain.id, thumbnail_path=f"cache/thumbnails/{plain.id}.jpg")
        _set_item(
            sessionmaker,
            tiled.id,
            thumbnail_path=f"cache/thumbnails/{tiled.id}.jpg",
            meta={"tiles": {"format": "dzi"}, "tags": ["keep"]},
        )
        self._derived(tmp_path, plain.id, tiles=False)
        self._derived(tmp_path, tiled.id, tiles=True)
        # Another project's derived data in the same container stays.
        stranger = uuid.uuid4()
        self._derived(tmp_path, stranger, tiles=True)
        queue = FakeJobQueue()
        ctx["queue"] = queue

        job = _seed_job(sessionmaker, fx.project.id, JobType.REBUILD_CACHE, {"purge": True})
        result = _run(jobs.rebuild_cache(ctx, str(job.id)))

        assert result["cleared_thumbnails"] == 2
        assert result["cleared_tiles"] == 1
        assert result["purged_blobs"] == 4  # two thumbnails, one tile, one .dzi
        assert result["purge_errors"] == []
        assert not (tmp_path / "cache" / "thumbnails" / f"{plain.id}.jpg").exists()
        assert not any((tmp_path / "cache" / "tiles" / str(tiled.id)).rglob("*.*"))
        assert (tmp_path / "cache" / "thumbnails" / f"{stranger}.jpg").is_file()
        assert (tmp_path / "cache" / "tiles" / str(stranger) / "image.dzi").is_file()

        assert _load_item(sessionmaker, plain.id).thumbnail_path is None
        stored = _load_item(sessionmaker, tiled.id)
        assert stored.thumbnail_path is None
        assert stored.meta == {"tags": ["keep"]}
        assert _load_item(sessionmaker, text.id).meta == {}

        thumb_id = uuid.UUID(result["thumbnail_job_id"])
        tile_id = uuid.UUID(result["tile_job_id"])
        assert queue.enqueued == [(thumb_id, "thumbnail"), (tile_id, "tile_image")]
        assert _load_job(sessionmaker, thumb_id).payload == {}
        assert _load_job(sessionmaker, tile_id).payload == {"item_ids": [str(tiled.id)]}
        finished = _load_job(sessionmaker, job.id)
        assert finished.status is JobStatus.SUCCEEDED
        assert finished.result is not None
        assert finished.result["tile_job_id"] == str(tile_id)

    def test_without_purge_blobs_stay_and_no_tile_job_without_tiles(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        item = _seed_item(sessionmaker, fx, "images/a.png")
        _set_item(sessionmaker, item.id, thumbnail_path=f"cache/thumbnails/{item.id}.jpg")
        self._derived(tmp_path, item.id, tiles=False)
        queue = FakeJobQueue()
        ctx["queue"] = queue

        job = _seed_job(sessionmaker, fx.project.id, JobType.REBUILD_CACHE)
        result = _run(jobs.rebuild_cache(ctx, str(job.id)))

        assert result["purged_blobs"] == 0
        assert result["tile_job_id"] is None
        assert (tmp_path / "cache" / "thumbnails" / f"{item.id}.jpg").is_file()
        assert [kind for _, kind in queue.enqueued] == ["thumbnail"]

    def test_an_unreachable_queue_leaves_the_follow_ups_queued_for_the_stranded_cron(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        _seed_item(sessionmaker, fx, "images/a.png")
        ctx["queue"] = FakeJobQueue(fail_with=QueueUnavailableError("down"))

        job = _seed_job(sessionmaker, fx.project.id, JobType.REBUILD_CACHE)
        result = _run(jobs.rebuild_cache(ctx, str(job.id)))

        assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED
        follow_up = _load_job(sessionmaker, uuid.UUID(result["thumbnail_job_id"]))
        assert follow_up.status is JobStatus.QUEUED

    def test_fails_without_a_queue_or_a_cache_connector(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        job = _seed_job(sessionmaker, fx.project.id, JobType.REBUILD_CACHE)
        with pytest.raises(LookupError):
            _run(jobs.rebuild_cache(ctx, str(job.id)))

        async def _detach() -> None:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                project.result_connector_id = None
                await session.commit()

        _run(_detach())
        ctx["queue"] = FakeJobQueue()
        with pytest.raises(LookupError):
            _run(jobs.rebuild_cache(ctx, str(job.id)))
        assert _load_job(sessionmaker, job.id).status is JobStatus.FAILED


def test_the_pod_and_compose_grace_periods_outlast_the_shutdown_job_wait() -> None:
    """A worker killed before its wait ends leaves jobs `running` until the stranded cron."""
    import re

    from app.worker.main import SHUTDOWN_JOB_WAIT_SECONDS, WorkerSettings

    assert WorkerSettings.job_completion_wait == SHUTDOWN_JOB_WAIT_SECONDS
    root = Path(__file__).resolve().parents[2]
    chart = root / "infra/helm/annotide/templates/worker.yaml"
    compose = root / "docker-compose.yml"
    if not chart.is_file() or not compose.is_file():
        pytest.skip("repository checkout not available")
    grace = re.search(r"terminationGracePeriodSeconds: (\d+)", chart.read_text())
    stop = re.search(r"worker:.*?stop_grace_period: (\d+)s", compose.read_text(), re.DOTALL)
    assert grace is not None and int(grace.group(1)) > SHUTDOWN_JOB_WAIT_SECONDS
    assert stop is not None and int(stop.group(1)) > SHUTDOWN_JOB_WAIT_SECONDS
