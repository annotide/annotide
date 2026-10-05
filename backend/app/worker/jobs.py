"""Job bodies for the arq worker, one per `job_type` (ARC-4).

Every function here has the same shape: arq calls it with the *database id*
of a `job` row (see `app.services.queue`), :func:`run_job` loads the row and
does the status/progress/attempt bookkeeping, and a small `work` coroutine in
between does the actual thing using the same services the API uses.

Retry policy: an unexpected exception re-queues the job with exponential
backoff until `APP_JOB_MAX_TRIES` is reached; a *permanent* failure (a
missing project, a connector config that cannot be built, a job type that is
not implemented) fails the job immediately, because retrying it would only
delay the same error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from arq import Retry
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ValidationFailedError
from app.connectors.base import StorageConnector
from app.connectors.errors import ConnectorConfigError, ConnectorError
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.exporters.base import (
    entity_set_for_item,
    pdf_document,
    split_image_items,
    split_text_items,
)
from app.exporters.llm import llm_records
from app.exporters.segments import segment_records
from app.importers import get_importer
from app.models import (
    Annotation,
    AnnotationSource,
    AnnotationStatus,
    Connector,
    Item,
    ItemStatus,
    Job,
    JobStatus,
    JobType,
    MediaType,
    Model,
    ModelTask,
    ModelVersion,
    Project,
    Snapshot,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas import AnnotationResult, LabelSchemaDefinition
from app.schemas.pdf_text import PdfWord
from app.services import derived_cache, scanning, tiling
from app.services.annotations import create_version
from app.services.datasets import (
    SPLIT_NAMES,
    DatasetEntry,
    DatasetFilter,
    SplitConfig,
    SplitName,
    build_export_archive,
    build_snapshot_files,
    export_blob_path,
    load_entries,
    manifest_annotation_ids,
    manifest_splits,
    resolve_schema_version,
    select_dataset,
    snapshot_blob_prefix,
    to_export_items,
)
from app.services.importing import (
    ImportTally,
    ItemIndex,
    convert_item,
    read_import_files,
)
from app.services.item_flow import load_workflow
from app.services.jobs import submit_job
from app.services.models import (
    MAX_PREDICT_ITEMS,
    ModelClient,
    ModelRejected,
    ModelUnavailable,
    PredictItem,
    map_result,
    model_facing_schema,
    normalise_mapping,
)
from app.services.pdf_text import extract_pdf_document, extract_pdf_words, group_ocr_lines
from app.services.queue import ArqJobQueue, JobQueue, QueueUnavailableError
from app.services.secrets import SecretResolutionError
from app.services.storage import storage_for
from app.services.tasks import open_annotate_tasks
from app.services.text_sources import (
    PdfTextStatus,
    TextSource,
    pdf_text_meta,
    pdf_text_path,
    text_source,
)
from app.services.thumbnails import (
    THUMBNAIL_CONTENT_TYPE,
    ThumbnailError,
    render_pdf_thumbnail,
    render_thumbnail,
    select_items,
    thumbnail_blob_path,
)
from app.services.uncertainty import uncertainty_priority, uncertainty_score
from app.services.webhooks import emit_event
from app.services.workflow import Trigger, next_status

log = get_logger(__name__)

ProgressReporter = Callable[[int], Awaitable[None]]
JobWork = Callable[[AsyncSession, Job, ProgressReporter], Awaitable[dict[str, Any]]]

#: Exceptions that will not get better by waiting. No retry.
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    NotImplementedError,
    LookupError,
    ConnectorConfigError,
    SecretResolutionError,
    ModelRejected,
    ValueError,
)

#: `/predict` calls stay well under the model's own limit so a slow model
#: still reports progress regularly on a large selection.
_PRELABEL_BATCH_SIZE = min(MAX_PREDICT_ITEMS, 32)

_RETRY_BASE_DELAY_S = 5
_RETRY_MAX_DELAY_S = 300


def backoff_seconds(attempt: int) -> float:
    """5s, 10s, 20s, ... capped at five minutes."""
    return float(min(_RETRY_BASE_DELAY_S * (2 ** max(attempt - 1, 0)), _RETRY_MAX_DELAY_S))


async def _set(sessionmaker: async_sessionmaker[AsyncSession], job_id: UUID, **fields: Any) -> None:
    """Update a few columns of the job row in its own short transaction."""
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        for name, value in fields.items():
            setattr(job, name, value)
        if fields.get("status") in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            await _emit_job_event(session, job)
        await session.commit()


#: A job reports progress at most this often; the rest are dropped. Loops call
#: `report` once per item, and each write is its own transaction on a second
#: pooled connection next to the job's own session.
PROGRESS_MIN_INTERVAL_SECONDS = 1.0


def _progress_reporter(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: UUID
) -> ProgressReporter:
    """A throttled `report(percent)` for one run of one job.

    Writes only a changed value, at most once per `PROGRESS_MIN_INTERVAL_SECONDS`,
    as one UPDATE that touches only a `running` row, so a late write cannot
    land on a job that was cancelled meanwhile. Progress is committed apart
    from the job's own session so the API sees it while the job runs; the
    final 100 comes with the outcome.
    """
    last_value = -1
    last_at = float("-inf")

    async def report(progress: int) -> None:
        nonlocal last_value, last_at
        value = max(0, min(100, progress))
        now = time.monotonic()
        if value == last_value or now - last_at < PROGRESS_MIN_INTERVAL_SECONDS:
            return
        last_value, last_at = value, now
        async with sessionmaker() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
                .values(progress=value)
            )
            await session.commit()

    return report


async def _emit_job_event(session: AsyncSession, job: Job) -> None:
    """`job.succeeded` / `job.failed` for subscribers (API-4), in the same commit as the status."""
    if job.project_id is None:
        return
    project = await session.get(Project, job.project_id)
    if project is None:
        return
    await emit_event(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        event=f"job.{job.status.value}",
        payload={
            "job_id": str(job.id),
            "type": job.type.value,
            "status": job.status.value,
            "result": job.result,
            "error": job.error,
        },
    )


async def _after_cancel(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: UUID, bound: Any
) -> None:
    """Record why a running job was cancelled: a user, or the worker stopping.

    `POST /jobs/{id}/cancel` marks the row `cancelled` before it signals the
    worker, so that row is already final. Anything else is the worker shutting
    down (a deploy, a scale-down, a node drain): arq puts the job back in the
    queue ("will be run again"), so the row goes back to `queued` — writing
    `cancelled` here made the re-run skip a job nobody had cancelled.
    """
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        if job.status is JobStatus.CANCELLED:
            bound.info("job.cancelled")
            job.finished_at = datetime.now(UTC)
        else:
            bound.info("job.interrupted", reason="worker shutdown")
            job.status = JobStatus.QUEUED
            job.progress = 0
        await session.commit()


#: How long a job whose project is at its running-job limit waits before
#: arq offers it again (APP_JOB_MAX_RUNNING_PER_PROJECT).
CAPACITY_RETRY_SECONDS = 15


async def _project_is_full(
    session: AsyncSession, job: Job, *, limit: int, timeout_seconds: int
) -> bool:
    """Whether `job`'s project already runs `limit` jobs. Locks the project row
    so two workers cannot both take its last slot; a `running` row older than
    the job timeout is a dead worker's and does not count."""
    if job.project_id is None:
        return False
    await session.execute(select(Project.id).where(Project.id == job.project_id).with_for_update())
    running = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.project_id == job.project_id,
            Job.id != job.id,
            Job.status == JobStatus.RUNNING,
            Job.started_at > datetime.now(UTC) - timedelta(seconds=timeout_seconds),
        )
    )
    return bool(running and running >= limit)


async def run_job(ctx: dict[str, Any], job_id: str, work: JobWork) -> dict[str, Any]:
    """Bookkeeping around `work`: claim the row, run, record the outcome.

    A job whose project is at `APP_JOB_MAX_RUNNING_PER_PROJECT` is not
    started: it stays `queued` and arq tries it again in
    :data:`CAPACITY_RETRY_SECONDS`. That wait costs no attempt — the failure
    limit counts `job.attempts`, which only real runs increment, and arq's own
    try ceiling (`WorkerSettings.max_tries`) leaves room for the waits.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    settings = ctx.get("settings") or get_settings()
    max_tries = int(getattr(settings, "job_max_tries", get_settings().job_max_tries))
    row_id = UUID(job_id)
    bound = log.bind(job_id=job_id)

    async with sessionmaker() as session:
        job = await session.get(Job, row_id)
        if job is None:
            bound.warning("job.missing")
            return {"skipped": "no such job"}
        if job.status is JobStatus.CANCELLED:
            bound.info("job.skipped_cancelled")
            return {"skipped": "cancelled"}
        limit = int(
            getattr(
                settings,
                "job_max_running_per_project",
                get_settings().job_max_running_per_project,
            )
        )
        timeout_seconds = int(ctx.get("job_timeout_seconds") or 1800)
        if await _project_is_full(session, job, limit=limit, timeout_seconds=timeout_seconds):
            await session.commit()  # release the project lock
            bound.info("job.waiting_for_slot", project_id=str(job.project_id), limit=limit)
            raise Retry(defer=CAPACITY_RETRY_SECONDS)
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        job.attempts = job.attempts + 1
        job.error = None
        await session.commit()
        job_type = job.type.value
        attempt = job.attempts
    bound = bound.bind(attempt=attempt)

    bound = bound.bind(job_type=job_type)
    bound.info("job.start")

    report = _progress_reporter(sessionmaker, row_id)

    started = time.monotonic()
    try:
        async with sessionmaker() as session:
            job = await session.get(Job, row_id)
            assert job is not None  # loaded a moment ago
            result = await work(session, job, report)
    except asyncio.CancelledError:
        await asyncio.shield(_after_cancel(sessionmaker, row_id, bound))
        raise
    except PERMANENT_ERRORS as exc:
        bound.warning("job.failed", error=str(exc), permanent=True)
        await _set(
            sessionmaker,
            row_id,
            status=JobStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )
        raise
    except Exception as exc:  # job boundary: everything is recorded
        bound.warning("job.failed", error=str(exc), permanent=False)
        if attempt >= max_tries:
            await _set(
                sessionmaker,
                row_id,
                status=JobStatus.FAILED,
                error=f"{type(exc).__name__}: {exc} (after {attempt} attempts)",
                finished_at=datetime.now(UTC),
            )
            raise
        await _set(sessionmaker, row_id, status=JobStatus.QUEUED, error=str(exc))
        raise Retry(defer=backoff_seconds(attempt)) from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    result = {**result, "duration_ms": duration_ms}
    await _set(
        sessionmaker,
        row_id,
        status=JobStatus.SUCCEEDED,
        progress=100,
        result=json.loads(json.dumps(result, default=str)),
        finished_at=datetime.now(UTC),
    )
    bound.info("job.done", duration_ms=duration_ms)
    return result


# --------------------------------------------------------------------------- #
# Shared lookups
# --------------------------------------------------------------------------- #


async def _project(session: AsyncSession, job: Job) -> Project:
    if job.project_id is None:
        raise LookupError("this job has no project")
    project = await session.get(Project, job.project_id)
    if project is None:
        raise LookupError(f"project {job.project_id} does not exist")
    return project


async def _connector(session: AsyncSession, connector_id: UUID | None, role: str) -> Connector:
    if connector_id is None:
        raise LookupError(f"the project has no {role} connector configured")
    connector = await session.get(Connector, connector_id)
    if connector is None:
        raise LookupError(f"{role} connector {connector_id} does not exist")
    return connector


# --------------------------------------------------------------------------- #
# job_type: scan_source (SRC-2)
# --------------------------------------------------------------------------- #


async def _scan_source(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    project = await _project(session, job)
    connector_id = payload.get("connector_id")
    connector = await _connector(
        session,
        UUID(str(connector_id)) if connector_id else project.source_connector_id,
        "source",
    )
    prefix = str(payload.get("prefix") or project.source_prefix or "")
    glob = payload.get("glob") or project.source_glob

    paths = payload.get("paths")

    await report(5)
    async with storage_for(connector) as storage:
        if isinstance(paths, list):
            # A store event named the objects (SRC-3): look those up, list nothing.
            result = await scanning.scan_paths(
                session,
                project_id=project.id,
                connector=connector,
                storage=storage,
                paths=[str(path) for path in paths],
            )
        else:
            result = await scanning.scan_source(
                session,
                project_id=project.id,
                connector=connector,
                storage=storage,
                prefix=prefix,
                glob=str(glob) if glob else None,
            )
        await session.commit()
    return result.as_dict()


async def scan_source(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """List the project's source connector and upsert `item` rows.

    Payload: `{"connector_id"?, "prefix"?, "glob"?}` — each overrides the
    project's own setting — or `{"paths": [...], "trigger": "event"}` from a
    storage event (SRC-3), which upserts just those objects. When the scan
    created items and the project has a result connector to write into, a
    `thumbnail` job is queued for them (IMG-8); its id is reported as
    `thumbnail_job_id`. When the project then
    has an untiled image item over `APP_THUMBNAIL_MAX_SOURCE_BYTES`, with
    width x height >= `APP_TILE_MIN_PIXELS`, or with no measured size, a
    `tile_image` job is queued too (IMG-1); its id is reported as
    `tile_job_id`. New PDFs of a project in PDF text mode queue an
    `extract_text` job (`extract_text_job_id`).
    """
    result = await run_job(ctx, job_id, _scan_source)
    # Every scan retries PDF texts still pending or failed, not only new ones.
    text_job_id = await _queue_extract_text_after_scan(ctx, UUID(job_id))
    if text_job_id is not None:
        result["extract_text_job_id"] = str(text_job_id)
    if not result.get("items_created"):
        return result
    thumbnail_job_id = await _queue_thumbnails_after_scan(ctx, UUID(job_id))
    if thumbnail_job_id is not None:
        result["thumbnail_job_id"] = str(thumbnail_job_id)
    tile_job_id = await _queue_tile_after_scan(ctx, UUID(job_id))
    if tile_job_id is not None:
        result["tile_job_id"] = str(tile_job_id)
    return result


def _queue_from_ctx(ctx: dict[str, Any]) -> JobQueue | None:
    """The queue a job body uses to chain another job: injected, else arq's own pool."""
    queue: JobQueue | None = ctx.get("queue")
    if queue is None and ctx.get("redis") is not None:
        queue = ArqJobQueue(ctx["redis"])
    return queue


async def _queue_thumbnails_after_scan(ctx: dict[str, Any], scan_job_id: UUID) -> UUID | None:
    queue = _queue_from_ctx(ctx)
    if queue is None:
        log.info("thumbnail.chain_skipped", reason="no queue in context", job_id=str(scan_job_id))
        return None
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    async with sessionmaker() as session:
        scan = await session.get(Job, scan_job_id)
        if scan is None or scan.project_id is None:
            return None
        project = await session.get(Project, scan.project_id)
        if project is None or project.effective_cache_connector_id is None:
            log.info(
                "thumbnail.chain_skipped", reason="no cache connector", job_id=str(scan_job_id)
            )
            return None
        job = await submit_job(
            session,
            queue,
            project_id=project.id,
            job_type=JobType.THUMBNAIL,
            payload={"after_scan_job_id": str(scan_job_id)},
        )
        return job.id


async def _queue_extract_text_after_scan(ctx: dict[str, Any], scan_job_id: UUID) -> UUID | None:
    """Chain an `extract_text` job when the project has PDF text items whose text
    is not ready: new ones from this scan, or ones an earlier run left pending
    or failed.
    """
    queue = _queue_from_ctx(ctx)
    if queue is None:
        log.info("extract_text.chain_skipped", reason="no queue in context")
        return None
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    async with sessionmaker() as session:
        scan = await session.get(Job, scan_job_id)
        if scan is None or scan.project_id is None:
            return None
        # Filtered in Python: JSONB paths are not portable to the SQLite tests.
        texts = await session.scalars(
            select(Item).where(
                Item.project_id == scan.project_id,
                Item.media_type == MediaType.TEXT,
            )
        )
        if not any(
            (meta := pdf_text_meta(item)) is not None and meta.get("status") != PdfTextStatus.READY
            for item in texts
        ):
            return None
        job = await submit_job(
            session,
            queue,
            project_id=scan.project_id,
            job_type=JobType.EXTRACT_TEXT,
            payload={"after_scan_job_id": str(scan_job_id)},
        )
        return job.id


async def _queue_tile_after_scan(ctx: dict[str, Any], scan_job_id: UUID) -> UUID | None:
    """Chain a `tile_image` job when the scan created a large image item (IMG-1)."""
    queue = _queue_from_ctx(ctx)
    if queue is None:
        log.info("tile_image.chain_skipped", reason="no queue in context", job_id=str(scan_job_id))
        return None
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    settings = get_settings()
    async with sessionmaker() as session:
        scan = await session.get(Job, scan_job_id)
        if scan is None or scan.project_id is None:
            return None
        project = await session.get(Project, scan.project_id)
        if project is None or project.effective_cache_connector_id is None:
            log.info(
                "tile_image.chain_skipped", reason="no cache connector", job_id=str(scan_job_id)
            )
            return None
        # Large by size or by measured pixels, or not measured at all (TIFF
        # and other formats the scan cannot size cheaply): the job measures
        # those itself and skips the small ones. Already tiled items never
        # count, so a scan that only added small images queues nothing.
        candidates = await session.scalars(
            select(Item).where(
                Item.project_id == project.id,
                Item.media_type == MediaType.IMAGE,
                or_(
                    Item.size_bytes > settings.thumbnail_max_source_bytes,
                    Item.width.is_(None),
                    Item.height.is_(None),
                    (Item.width * Item.height) >= settings.tile_min_pixels,
                ),
            )
        )
        needs_tiling = next(
            (item.id for item in candidates if "tiles" not in (item.meta or {})), None
        )
        if needs_tiling is None:
            log.info("tile_image.chain_skipped", reason="no large images", job_id=str(scan_job_id))
            return None
        job = await submit_job(
            session,
            queue,
            project_id=project.id,
            job_type=JobType.TILE_IMAGE,
            payload={"after_scan_job_id": str(scan_job_id)},
        )
        return job.id


# --------------------------------------------------------------------------- #
# job_type: thumbnail (IMG-8, UX-6)
# --------------------------------------------------------------------------- #

#: Rows committed (and progress reported) per batch of thumbnails.
_THUMBNAIL_BATCH_SIZE = 20


async def _thumbnail(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    settings = get_settings()
    project = await _project(session, job)
    cache_connector = await _connector(session, project.effective_cache_connector_id, "cache")
    raw_ids = payload.get("item_ids")
    item_ids = [UUID(str(value)) for value in raw_ids] if isinstance(raw_ids, list) else None
    items = await select_items(
        session, project.id, force=bool(payload.get("force")), item_ids=item_ids
    )

    written = 0
    too_large = 0
    errors: list[str] = []
    await report(1)
    if not items:
        return {"selected": 0, "written": 0, "skipped_too_large": 0, "errors": errors}

    source_connectors: dict[UUID, Connector] = {}
    for item in items:
        if item.connector_id not in source_connectors:
            source_connectors[item.connector_id] = await _connector(
                session, item.connector_id, "source"
            )

    async with storage_for(cache_connector) as cache_storage:
        # One source connector at a time so its credential resolves once.
        for connector_id, connector in source_connectors.items():
            async with storage_for(connector) as source_storage:
                for index, item in enumerate(items, start=1):
                    if item.connector_id != connector_id:
                        continue
                    if item.size_bytes > settings.thumbnail_max_source_bytes:
                        too_large += 1
                        continue
                    try:
                        data = await source_storage.read(item.path)
                        if item.media_type is MediaType.PDF:
                            rendered = await asyncio.to_thread(
                                render_pdf_thumbnail, data, settings.thumbnail_size
                            )
                            jpeg = rendered.jpeg
                            item.meta = {**(item.meta or {}), **rendered.meta}
                        else:
                            jpeg = await asyncio.to_thread(
                                render_thumbnail, data, settings.thumbnail_size
                            )
                        path = thumbnail_blob_path(item.id)
                        await cache_storage.write(path, jpeg, THUMBNAIL_CONTENT_TYPE)
                    except (ThumbnailError, ConnectorError) as exc:
                        errors.append(f"{item.path}: {exc}")
                        continue
                    item.thumbnail_path = path
                    written += 1
                    if written % _THUMBNAIL_BATCH_SIZE == 0:
                        await session.commit()
                        await report(int(index / len(items) * 100))
        await session.commit()
    return {
        "selected": len(items),
        "written": written,
        "skipped_too_large": too_large,
        "errors": errors,
    }


async def thumbnail(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Generate `cache/thumbnails/{item_id}.jpg` for the project's image and PDF items.

    PDF items are thumbnailed from page 1 and also get `meta.page_count`,
    `meta.page_sizes` and `meta.text_layer`.

    Payload: `{"force"?: bool, "item_ids"?: [uuid]}`. Undecodable files and
    files over `APP_THUMBNAIL_MAX_SOURCE_BYTES` are counted, not fatal: one
    corrupt upload must not block the whole grid.
    """
    return await run_job(ctx, job_id, _thumbnail)


# --------------------------------------------------------------------------- #
# job_type: extract_text (PDF text mode)
# --------------------------------------------------------------------------- #

_PDF_TEXT_MAX_SOURCE_BYTES = 64 * 1024 * 1024


async def _ocr_model(session: AsyncSession, organization_id: UUID) -> Model | None:
    """The organisation's first `ocr` model with an endpoint (the annotator's choice)."""
    model: Model | None = await session.scalar(
        select(Model)
        .where(
            Model.organization_id == organization_id,
            Model.task == ModelTask.OCR,
            Model.endpoint_url.is_not(None),
            Model.deleted_at.is_(None),
        )
        .order_by(Model.created_at, Model.id)
        .limit(1)
    )
    return model


#: OCR budget per item: a long scan, or a model that keeps failing, must not
#: hold the worker for hours. Pages past either limit stay empty.
_OCR_MAX_PAGES_PER_ITEM = 200
_OCR_MAX_FAILURES_IN_A_ROW = 3


async def _extract_item_text(
    item: Item,
    source: StorageConnector,
    ocr: ModelClient | None,
    settings: Settings,
    errors: list[str],
    result_connector_id: UUID,
) -> tuple[str, dict[str, Any]]:
    """The document text of one PDF item and its `meta.pdf_text` (ready).

    Raises `ConnectorError` / `ValueError` (incl. `PdfTextError`) for a file
    that cannot be read.
    """
    if item.size_bytes > _PDF_TEXT_MAX_SOURCE_BYTES:
        raise ValueError("over 64 MiB")
    data = await source.read(item.path)
    if len(data) > _PDF_TEXT_MAX_SOURCE_BYTES:
        raise ValueError("over 64 MiB")
    page_count, words = await asyncio.to_thread(extract_pdf_document, data)
    by_page: dict[int, list[PdfWord]] = {}
    for word in words:
        by_page.setdefault(word.page, []).append(word)

    ocr_pages: list[int] = []
    empty_pages: list[int] = []
    url: str | None = None
    attempts = failures_in_a_row = 0
    for page in range(1, page_count + 1):
        if page in by_page:
            continue
        if (
            ocr is None
            or attempts >= _OCR_MAX_PAGES_PER_ITEM
            or failures_in_a_row >= _OCR_MAX_FAILURES_IN_A_ROW
        ):
            empty_pages.append(page)
            continue
        attempts += 1
        try:
            if url is None:
                url = await source.signed_url(
                    item.path, expires_in=settings.signed_url_ttl, internal=True
                )
            read = await ocr.ocr(
                PredictItem(id=str(item.id), url=url, width=0, height=0, media_type="pdf"), page
            )
        except (ModelUnavailable, ModelRejected, SecretResolutionError, ConnectorError) as exc:
            errors.append(f"{item.path}: page {page}: OCR failed: {exc}")
            empty_pages.append(page)
            failures_in_a_row += 1
            continue
        failures_in_a_row = 0
        lines = group_ocr_lines(page, read.words)
        if lines:
            by_page[page] = lines
            ocr_pages.append(page)
        else:
            empty_pages.append(page)

    ordered = [word for page in sorted(by_page) for word in by_page[page]]
    text, ranges = pdf_document(ordered)
    spans: dict[int, list[tuple[int, int]]] = {}
    for word, span in zip(ordered, ranges, strict=True):
        spans.setdefault(word.page, []).append(span)
    pages: list[list[int]] = []
    position = 0
    for page in range(1, page_count + 1):
        if page in spans:
            position = spans[page][-1][1]
            pages.append([spans[page][0][0], position])
        else:
            pages.append([position, position])
    meta = {
        "status": PdfTextStatus.READY.value,
        "path": pdf_text_path(item.id),
        "connector_id": str(result_connector_id),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "pages": pages,
        "ocr_pages": ocr_pages,
        "empty_pages": empty_pages,
    }
    return text, meta


async def _extract_text(
    session: AsyncSession, job: Job, report: ProgressReporter
) -> dict[str, Any]:
    payload = job.payload
    settings = get_settings()
    project = await _project(session, job)
    result_connector = await _connector(session, project.result_connector_id, "result")
    raw_ids = payload.get("item_ids")
    wanted = {UUID(str(value)) for value in raw_ids} if isinstance(raw_ids, list) else None
    force = bool(payload.get("force"))

    candidates = [
        item
        for item in await session.scalars(
            select(Item)
            .where(Item.project_id == project.id, Item.media_type == MediaType.TEXT)
            .order_by(Item.created_at, Item.id)
        )
        if (pdf_text_meta(item) is not None) and (wanted is None or item.id in wanted)
    ]
    annotated: set[UUID] = set()
    if force and candidates:
        annotated = set(
            await session.scalars(
                select(Annotation.item_id).where(Annotation.item_id.in_([i.id for i in candidates]))
            )
        )
    items: list[Item] = []
    skipped_annotated = 0
    for item in candidates:
        meta = pdf_text_meta(item) or {}
        if meta.get("status") != PdfTextStatus.READY:
            items.append(item)
        elif force:
            if item.id in annotated:
                skipped_annotated += 1
            else:
                items.append(item)

    result: dict[str, Any] = {
        "selected": len(items),
        "extracted": 0,
        "failed": 0,
        "skipped_annotated": skipped_annotated,
        "ocr_pages": 0,
        "empty_pages": 0,
        "tasks_opened": 0,
        "errors": [],
    }
    errors: list[str] = result["errors"]
    await report(1)
    if not items:
        return result

    config = await load_workflow(session, project.id)
    ocr_model = await _ocr_model(session, project.organization_id)
    sources: dict[UUID, Connector] = {}
    for item in items:
        if item.connector_id not in sources:
            sources[item.connector_id] = await _connector(session, item.connector_id, "source")

    async with AsyncExitStack() as stack:
        result_storage = await stack.enter_async_context(storage_for(result_connector))
        ocr_client: ModelClient | None = None
        if ocr_model is not None:
            try:
                ocr_client = await stack.enter_async_context(
                    await ModelClient.for_model(
                        ocr_model.endpoint_url,
                        ocr_model.identity_type,
                        ocr_model.secret_ref,
                        ocr_model.identity_config,
                    )
                )
            except (ModelUnavailable, SecretResolutionError) as exc:
                errors.append(f"OCR model '{ocr_model.name}' unavailable: {exc}")
        for connector_id, connector in sources.items():
            async with storage_for(connector) as source:
                for index, item in enumerate(items, start=1):
                    if item.connector_id != connector_id:
                        continue
                    page_errors: list[str] = []
                    try:
                        text, meta = await _extract_item_text(
                            item, source, ocr_client, settings, page_errors, result_connector.id
                        )
                        await result_storage.write(
                            pdf_text_path(item.id),
                            text.encode("utf-8"),
                            "text/plain; charset=utf-8",
                        )
                    except (ConnectorError, ValueError) as exc:
                        item.meta = {
                            **(item.meta or {}),
                            "pdf_text": {
                                "status": PdfTextStatus.FAILED.value,
                                "path": pdf_text_path(item.id),
                                "error": str(exc)[:500],
                            },
                        }
                        result["failed"] += 1
                        errors.append(f"{item.path}: {exc}")
                    else:
                        item.meta = {**(item.meta or {}), "pdf_text": meta}
                        result["extracted"] += 1
                        result["ocr_pages"] += len(meta["ocr_pages"])
                        result["empty_pages"] += len(meta["empty_pages"])
                        errors.extend(page_errors)
                        has_task = await session.scalar(
                            select(Task.id).where(Task.item_id == item.id).limit(1)
                        )
                        if has_task is None:
                            opened = await open_annotate_tasks(
                                session, item_id=item.id, project_id=project.id, config=config
                            )
                            result["tasks_opened"] += len(opened)
                    await session.commit()
                    await report(int(index / len(items) * 100))
    result["errors"] = errors[:20]
    return result


async def extract_text(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Read the text of the project's PDF text items (PDF text mode).

    Payload: `{"item_ids"?: [uuid], "force"?: bool}`. Writes `text/{item_id}.txt`
    to the result connector, sets `meta.pdf_text` and opens the annotate tasks.
    An unreadable PDF is counted as `failed`, not fatal.
    """
    return await run_job(ctx, job_id, _extract_text)


# --------------------------------------------------------------------------- #
# job_type: snapshot (EXP-1, EXP-2)
# --------------------------------------------------------------------------- #


async def _snapshot(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    project = await _project(session, job)
    connector = await _connector(session, project.result_connector_id, "result")
    created_by = payload.get("created_by_id")
    if not created_by:
        raise LookupError("snapshot payload has no created_by_id")

    dataset_filter = DatasetFilter.model_validate(payload.get("filter") or {})
    version_id = payload.get("label_schema_version_id")
    schema_version = await resolve_schema_version(
        session, project, UUID(str(version_id)) if version_id else None
    )
    split = SplitConfig.model_validate(payload["split"]) if payload.get("split") else None
    entries = await select_dataset(session, project.id, dataset_filter)
    await report(30)

    snapshot_id = uuid4()
    name = str(payload.get("name") or f"snapshot-{datetime.now(UTC):%Y%m%d-%H%M%S}")
    files = build_snapshot_files(
        snapshot_id=snapshot_id,
        project_id=project.id,
        name=name,
        dataset_filter=dataset_filter,
        label_schema_version_id=schema_version.id,
        entries=entries,
        split=split,
    )
    prefix = snapshot_blob_prefix(snapshot_id)

    async with storage_for(connector) as storage:
        await storage.write(
            f"{prefix}annotations.jsonl", files.annotations_jsonl, "application/x-ndjson"
        )
        await report(70)
        await storage.write(
            f"{prefix}manifest.json",
            json.dumps(files.manifest, indent=2, sort_keys=True).encode("utf-8"),
            "application/json",
        )
    await report(90)

    session.add(
        Snapshot(
            id=snapshot_id,
            project_id=project.id,
            name=name,
            filter=dataset_filter.model_dump(mode="json", exclude_none=True),
            split=split.model_dump(mode="json") if split is not None else None,
            label_schema_version_id=schema_version.id,
            item_count=len(entries),
            blob_path=prefix,
            digest=files.digest,
            created_by_id=UUID(str(created_by)),
        )
    )
    await emit_event(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        event="snapshot.created",
        payload={
            "snapshot_id": str(snapshot_id),
            "name": name,
            "item_count": len(entries),
            "digest": files.digest,
            "blob_path": prefix,
            "label_schema_version_id": str(schema_version.id),
            "split": split.model_dump(mode="json") if split is not None else None,
        },
    )
    await session.commit()
    result: dict[str, Any] = {
        "snapshot_id": str(snapshot_id),
        "item_count": len(entries),
        "digest": files.digest,
        "blob_path": prefix,
    }
    if split is not None:
        result["split_counts"] = files.manifest["split"]["counts"]
    return result


async def snapshot(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Freeze the project's current annotations into an immutable snapshot.

    Payload: `{"name", "filter", "label_schema_version_id"?, "created_by_id"}`.
    Writes `snapshots/{id}/annotations.jsonl` + `manifest.json` to the result
    connector and inserts the `snapshot` row with the JSONL's sha256 digest.
    """
    return await run_job(ctx, job_id, _snapshot)


# --------------------------------------------------------------------------- #
# job_type: export (EXP-5)
# --------------------------------------------------------------------------- #


_IMAGE_ONLY_FORMATS = frozenset({"coco", "yolo"})

#: spaCy / CoNLL understand `text` and `pdf` items (CONTRACTS.md, text export formats).
_TEXT_FORMATS = frozenset({"spacy", "conll"})

#: `llm` reads each `llm` item's JSON document the same way (§5 LLM-data).
_LLM_FORMATS = frozenset({"llm"})

#: Text items over this size are skipped with a warning rather than read in full.
_TEXT_EXPORT_MAX_SOURCE_BYTES = 16 * 1024 * 1024

#: pdf items over this size are skipped the same way (CONTRACTS.md *PDF items*).
_PDF_EXPORT_MAX_SOURCE_BYTES = 64 * 1024 * 1024


async def _read_sources[T](
    session: AsyncSession,
    project: Project,
    entries: list[DatasetEntry],
    media_type: MediaType,
    max_bytes: int,
    decode: Callable[[bytes], Awaitable[T]],
) -> dict[UUID, T]:
    """Read and `decode` each `media_type` item's source, keyed by item id.

    A PDF in text mode is read from its extracted text (`text_source`,
    CONTRACTS.md *PDF text mode*); one whose text is not ready is left out.
    Items over `max_bytes` (the PDF's size does not count for its text), that
    fail to read, or whose `decode` raises `ValueError` are simply left out of
    the returned map; `split_text_items` turns a missing item into a warning,
    so there is exactly one message per skip.
    """
    decoded: dict[UUID, T] = {}
    sources: dict[UUID, TextSource] = {}
    for entry in entries:
        if entry.item.media_type is not media_type:
            continue
        if pdf_text_meta(entry.item) is None and entry.item.size_bytes > max_bytes:
            continue
        source = text_source(entry.item, project)
        if source is not None:
            sources[entry.item.id] = source
    connectors: dict[UUID, Connector] = {}
    for source in sources.values():
        if source.connector_id not in connectors:
            connectors[source.connector_id] = await _connector(
                session, source.connector_id, "source"
            )
    for connector_id, connector in connectors.items():
        async with storage_for(connector) as storage:
            for item_id, source in sources.items():
                if source.connector_id != connector_id:
                    continue
                try:
                    data = await storage.read(source.path)
                    decoded[item_id] = await decode(data)
                except (ConnectorError, ValueError):
                    continue
    return decoded


async def _decode_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


async def _decode_pdf(data: bytes) -> list[PdfWord]:
    # PdfTextError is a ValueError: undecodable and encrypted files are skipped.
    return await asyncio.to_thread(extract_pdf_words, data)


async def _read_text_sources(
    session: AsyncSession,
    project: Project,
    entries: list[DatasetEntry],
    media_type: MediaType = MediaType.TEXT,
) -> dict[UUID, str]:
    """Read each `media_type` item's source content: `text` for `spacy`/`conll`,
    `llm` for `llm` (EXP-5)."""
    return await _read_sources(
        session, project, entries, media_type, _TEXT_EXPORT_MAX_SOURCE_BYTES, _decode_text
    )


async def _read_pdf_words(
    session: AsyncSession, project: Project, entries: list[DatasetEntry]
) -> dict[UUID, list[PdfWord]]:
    """The text-layer words of each `pdf` item, for `spacy`/`conll` (EXP-5)."""
    return await _read_sources(
        session, project, entries, MediaType.PDF, _PDF_EXPORT_MAX_SOURCE_BYTES, _decode_pdf
    )


async def _export(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    project = await _project(session, job)
    connector = await _connector(session, project.result_connector_id, "result")
    export_format = str(payload.get("format") or "native")
    snapshot_id = payload.get("snapshot_id")
    only_split = payload.get("split")
    if only_split is not None and only_split not in SPLIT_NAMES:
        raise LookupError(f"unknown split {only_split!r}; expected one of {SPLIT_NAMES}")
    splits: dict[UUID, SplitName] | None = None

    async with storage_for(connector) as storage:
        if snapshot_id:
            frozen = await session.get(Snapshot, UUID(str(snapshot_id)))
            if frozen is None or frozen.project_id != project.id:
                raise LookupError(f"snapshot {snapshot_id} does not exist in this project")
            manifest = json.loads(await storage.read(f"{frozen.blob_path}manifest.json"))
            entries = await load_entries(session, manifest_annotation_ids(manifest))
            schema_version = await resolve_schema_version(
                session, project, frozen.label_schema_version_id
            )
            source: dict[str, Any] = {"snapshot_id": str(frozen.id), "digest": frozen.digest}
            # A split snapshot exports as train/ val/ test/ directories, or
            # just one of them when the request names it (EXP-3). The
            # manifest's `split` key decides: an empty snapshot is still split.
            splits = manifest_splits(manifest) if "split" in manifest else None
            if only_split:
                if splits is None:
                    raise LookupError(f"snapshot {snapshot_id} has no train/val/test split")
                entries = [e for e in entries if splits.get(e.annotation.id) == only_split]
                splits = {e.annotation.id: splits[e.annotation.id] for e in entries}
                source["split"] = only_split
        else:
            if only_split:
                raise LookupError("`split` needs a `snapshot_id`; a filter export has no split")
            dataset_filter = DatasetFilter.model_validate(payload.get("filter") or {})
            version_id = payload.get("label_schema_version_id")
            schema_version = await resolve_schema_version(
                session, project, UUID(str(version_id)) if version_id else None
            )
            entries = await select_dataset(session, project.id, dataset_filter)
            source = {"filter": dataset_filter.model_dump(mode="json", exclude_none=True)}
        await report(40)

        texts: dict[UUID, str] | None = None
        pdf_words: dict[UUID, list[PdfWord]] | None = None
        if export_format in _TEXT_FORMATS:
            texts = await _read_text_sources(session, project, entries)
            pdf_words = await _read_pdf_words(session, project, entries)
        elif export_format in _LLM_FORMATS:
            texts = await _read_text_sources(session, project, entries, MediaType.LLM)

        definition = LabelSchemaDefinition.model_validate(schema_version.definition)
        blob_path = export_blob_path(job.id, export_format)
        archive = build_export_archive(
            export_format=export_format,
            entries=entries,
            definition=definition,
            splits=splits,
            texts=texts,
            pdf_words=pdf_words,
            manifest={
                "job_id": str(job.id),
                "project_id": str(project.id),
                "format": export_format,
                "label_schema_version_id": str(schema_version.id),
                "item_count": len(entries),
                "created_at": datetime.now(UTC).isoformat(),
                **source,
            },
        )
        await report(75)
        await storage.write(blob_path, archive, "application/zip")

    # COCO / YOLO skip text and video items; spaCy / CoNLL skip everything
    # else plus overlap-dropped spans. The job result says so too, not only
    # the `warnings.json` inside the archive (CONTRACTS.md, text items).
    warnings: list[str] = []
    if export_format in _IMAGE_ONLY_FORMATS:
        _, warnings = split_image_items(to_export_items(entries))
    elif export_format in _TEXT_FORMATS:
        text_items, warnings = split_text_items(
            to_export_items(entries, texts=texts, pdf_words=pdf_words)
        )
        for item in text_items:
            _, drop_warnings = entity_set_for_item(item)
            warnings.extend(drop_warnings)
    elif export_format in _LLM_FORMATS:
        _, warnings = llm_records(to_export_items(entries, texts=texts))
    elif export_format == "segments":
        _, _, warnings = segment_records(to_export_items(entries))
    return {
        "format": export_format,
        "blob_path": blob_path,
        "item_count": len(entries),
        "label_schema_version_id": str(schema_version.id),
        "warnings": warnings,
        **source,
    }


async def export(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Render annotations to COCO / YOLO / native and write one zip.

    Payload: `{"format", "filter"?, "snapshot_id"?, "label_schema_version_id"?}`.
    With `snapshot_id` the export covers exactly the snapshot's frozen
    annotation versions; otherwise the latest version per item matching
    `filter`. The archive lands at `exports/{job_id}/{format}.zip`.
    """
    return await run_job(ctx, job_id, _export)


# --------------------------------------------------------------------------- #
# job_type: prelabel (ML-2, ML-4, ML-10)
# --------------------------------------------------------------------------- #


async def _prelabel(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    project = await _project(session, job)

    version_id = payload.get("model_version_id")
    if not version_id:
        raise LookupError("prelabel payload has no model_version_id")
    version = await session.get(ModelVersion, UUID(str(version_id)))
    if version is None:
        raise LookupError(f"model version {version_id} does not exist")
    model = await session.get(Model, version.model_id)
    if model is None or model.deleted_at is not None:
        raise LookupError(f"model {version.model_id} does not exist")

    connector = await _connector(session, project.source_connector_id, "source")

    schema_version_id = payload.get("label_schema_version_id")
    schema_version = await resolve_schema_version(
        session, project, UUID(str(schema_version_id)) if schema_version_id else None
    )
    definition = LabelSchemaDefinition.model_validate(schema_version.definition)
    mapping = normalise_mapping(dict(version.class_mapping))
    model_schema = model_facing_schema(definition, mapping)

    dataset_filter = DatasetFilter.model_validate(payload.get("filter") or {})
    # The API validates this too; the clamp here keeps a hand-crafted payload
    # from ever selecting an item a human is working on (ML-10).
    statuses = [
        s
        for s in (dataset_filter.item_status or [ItemStatus.NEW, ItemStatus.PRELABELED])
        if s in (ItemStatus.NEW, ItemStatus.PRELABELED)
    ]
    confidence_threshold = float(str(payload.get("confidence_threshold") or 0.0))
    limit = payload.get("limit")
    prioritize = bool(payload.get("prioritize_uncertain", False))

    # ML-10: an item with a human annotation version is never touched,
    # whichever filter is given, so it is excluded from selection and
    # counted separately rather than silently dropped.
    human_items = select(Annotation.item_id).where(Annotation.source == AnnotationSource.HUMAN)

    # Images, text and PDFs (ML-2); which of them the model serves is asked below.
    base_conditions = [
        Item.project_id == project.id,
        Item.media_type.in_((MediaType.IMAGE, MediaType.TEXT, MediaType.PDF)),
    ]
    base_conditions.append(Item.status.in_(statuses))
    if dataset_filter.path_prefix:
        base_conditions.append(Item.path.startswith(dataset_filter.path_prefix))

    skipped_human = int(
        await session.scalar(
            select(func.count()).select_from(Item).where(*base_conditions, Item.id.in_(human_items))
        )
        or 0
    )

    # Idempotent per model version: an item this version already produced a
    # draft for is not sent again. That is what makes a `Retry` after a
    # partially committed run safe, and a re-run of the same version a no-op,
    # while a *newer* version still adds its own drafts (ML-10).
    done_items = select(Annotation.item_id).where(Annotation.author_model_version_id == version.id)
    skipped_done = int(
        await session.scalar(
            select(func.count())
            .select_from(Item)
            .where(*base_conditions, Item.id.notin_(human_items), Item.id.in_(done_items))
        )
        or 0
    )

    stmt = (
        select(Item)
        .where(*base_conditions, Item.id.notin_(human_items), Item.id.notin_(done_items))
        .order_by(Item.created_at, Item.id)
    )
    if limit:
        stmt = stmt.limit(int(str(limit)))
    items = list(await session.scalars(stmt))

    result: dict[str, Any] = {
        "model_version_id": str(version.id),
        "selected": len(items),
        "predicted": 0,
        "empty": 0,
        "skipped_human": skipped_human,
        "skipped_done": skipped_done,
        "errors": 0,
        "dropped_shapes": 0,
        "skipped_media": 0,
    }
    if prioritize:
        result["prioritized"] = 0
    if not items:
        return result

    async def _prioritize(target: Item, mapped: AnnotationResult | None) -> None:
        """ML-6: write the prediction's uncertainty into the item's queue slot."""
        score = uncertainty_score(mapped)
        target.meta = {**target.meta, "uncertainty": score}
        task = await session.scalar(
            select(Task).where(
                Task.item_id == target.id,
                Task.type == TaskType.ANNOTATE,
                Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
            )
        )
        if task is not None:
            task.priority = uncertainty_priority(score)
            result["prioritized"] += 1

    settings = get_settings()
    total = len(items)
    processed = 0

    async with (
        storage_for(connector) as storage,
        await ModelClient.for_model(
            model.endpoint_url, model.identity_type, model.secret_ref, model.identity_config
        ) as client,
        AsyncExitStack() as stack,
    ):
        # A PDF in text mode is read from its extracted text on the result
        # connector (CONTRACTS.md *PDF text mode*), opened on first use.
        storages: dict[UUID, StorageConnector] = {connector.id: storage}
        served = await _served_media_types(client)
        for start in range(0, total, _PRELABEL_BATCH_SIZE):
            batch = items[start : start + _PRELABEL_BATCH_SIZE]
            predict_items: list[PredictItem] = []
            for item in batch:
                if item.media_type.value not in served:
                    # A text item and an image-only model (or the reverse):
                    # counted, never sent (CONTRACTS.md, text pre-labelling).
                    result["skipped_media"] += 1
                    continue
                source = text_source(item, project)
                if source is None:
                    # Its text is not extracted yet, or extraction failed.
                    result["skipped_media"] += 1
                    continue
                if source.connector_id not in storages:
                    storages[source.connector_id] = await stack.enter_async_context(
                        storage_for(await _connector(session, source.connector_id, "result"))
                    )
                # The model fetches it, not a browser: sign for the internal
                # endpoint, which a compose emulator's public name is not.
                url = await storages[source.connector_id].signed_url(
                    source.path, expires_in=settings.signed_url_ttl, internal=True
                )
                predict_items.append(
                    PredictItem(
                        id=str(item.id),
                        url=url,
                        width=item.width or 0,
                        height=item.height or 0,
                        media_type=item.media_type.value,
                    )
                )
            if not predict_items:
                processed += len(batch)
                await report(min(99, int(processed / total * 100)))
                continue
            predictions = await client.predict(
                predict_items, model_schema, confidence_threshold=confidence_threshold
            )
            items_by_id = {str(item.id): item for item in batch}
            for prediction in predictions:
                target = items_by_id.get(prediction.item_id)
                if target is None:
                    continue
                if prediction.error:
                    result["errors"] += 1
                    # The tally alone says nothing about *why*; the model's
                    # message (a URL it cannot fetch, a bad image) is the fix.
                    log.warning(
                        "prelabel.item_error",
                        job_id=str(job.id),
                        item_id=prediction.item_id,
                        error=prediction.error,
                    )
                    continue

                mapped, dropped = map_result(prediction.result, mapping, definition)
                result["dropped_shapes"] += dropped
                if prioritize:
                    await _prioritize(target, mapped)
                if not mapped.shapes and not mapped.classification:
                    result["empty"] += 1
                else:
                    await create_version(
                        session,
                        item=target,
                        result=mapped,
                        label_schema_version_id=schema_version.id,
                        author_model_version_id=version.id,
                        status=AnnotationStatus.DRAFT,
                    )
                    result["predicted"] += 1

                if target.status is ItemStatus.NEW:
                    target.status = next_status(target.status, Trigger.PRELABEL, role=None)

            processed += len(batch)
            await session.commit()
            await report(min(99, int(processed / total * 100)))

    return result


async def _served_media_types(client: ModelClient) -> set[str]:
    """Media types the model says it serves (`GET /info`), `{"image"}` when unsaid.

    Models written before text pre-labelling do not list `media_types`; they
    are image models. An unreachable `/info` fails the job like `/predict`
    would, so it is not caught here.
    """
    info = await client.info()
    declared = info.get("media_types") if isinstance(info, dict) else None
    if not isinstance(declared, list) or not declared:
        return {MediaType.IMAGE.value}
    return {str(value) for value in declared}


async def prelabel(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Run a model version over selected items to create `source=model` drafts.

    Payload: `{"model_version_id", "label_schema_version_id"?, "filter"?
    ({"item_status", "path_prefix"}), "limit"?, "confidence_threshold"?,
    "prioritize_uncertain"?}`. With `prioritize_uncertain` every scored
    item's open annotate task gets `priority = round(uncertainty * 100)`
    and `item.meta.uncertainty` is set, so the queue serves the model's
    least confident predictions first (ML-6).
    Image and text items are selected; those whose media type the model does
    not list in `GET /info` `media_types` (default `["image"]`) are counted in
    `skipped_media` and never sent. Items with a human annotation version are
    never touched (ML-10).
    Each non-empty prediction becomes a `draft` annotation authored by the
    model version, and a `new` item moves to `prelabeled`. `limit` is the
    dry run a customer runs before committing to a full pass (BYOM-7).
    """
    return await run_job(ctx, job_id, _prelabel)


# --------------------------------------------------------------------------- #
# job_type: import (EXP-6)
# --------------------------------------------------------------------------- #

#: Item statuses a `submitted` import advances to `submitted`. Items under or
#: past review keep their status: an import must not undo a reviewer's work.
_IMPORT_ADVANCES: frozenset[ItemStatus] = frozenset(
    {
        ItemStatus.NEW,
        ItemStatus.PRELABELED,
        ItemStatus.ANNOTATING,
        ItemStatus.SKIPPED,
        ItemStatus.REJECTED,
    }
)

_IMPORT_COMMIT_EVERY = 100


async def _import_annotations(
    session: AsyncSession, job: Job, report: ProgressReporter
) -> dict[str, Any]:
    payload = job.payload
    project = await _project(session, job)
    import_format = str(payload.get("format") or "")
    importer = get_importer(import_format)  # KeyError → LookupError → permanent

    path = str(payload.get("path") or "")
    if not path:
        raise LookupError("import payload has no path")
    connector_id = payload.get("connector_id") or project.source_connector_id
    connector = await _connector(
        session, UUID(str(connector_id)) if connector_id else None, "source"
    )
    created_by = payload.get("created_by_id")
    if not created_by:
        raise LookupError("import payload has no created_by_id")
    author_id = UUID(str(created_by))
    dry_run = bool(payload.get("dry_run", False))
    status = AnnotationStatus(str(payload.get("status") or AnnotationStatus.SUBMITTED))
    raw_mapping: Any = payload.get("class_mapping") or {}
    class_mapping = {str(k): str(v) for k, v in raw_mapping.items()}
    raw_attribute_mapping: Any = payload.get("attribute_mapping") or {}
    attribute_mapping = {
        str(cls): {str(k): None if v is None else str(v) for k, v in rules.items()}
        for cls, rules in raw_attribute_mapping.items()
    }

    schema_version_id = payload.get("label_schema_version_id")
    schema_version = await resolve_schema_version(
        session, project, UUID(str(schema_version_id)) if schema_version_id else None
    )
    definition = LabelSchemaDefinition.model_validate(schema_version.definition)

    async with storage_for(connector) as storage:
        files = await read_import_files(storage, path)
    records = list(importer.parse(files))
    await report(20)

    items = list(
        await session.scalars(
            select(Item).where(Item.project_id == project.id).order_by(Item.created_at, Item.id)
        )
    )
    index = ItemIndex.build(items)

    tally = ImportTally(parsed=len(records))
    total = len(records) or 1
    for position, record in enumerate(records, start=1):
        for shape in record.shapes:
            tally.note_shape(shape)

        item = index.resolve(record.path)
        if item is None:
            tally.note_unmatched(record.path)
            continue
        tally.matched += 1

        converted = convert_item(
            record,
            item,
            definition,
            class_mapping,
            schema_version=int(definition.version),
            attribute_mapping=attribute_mapping,
        )
        tally.dropped_shapes += converted.dropped_shapes
        tally.dropped_attributes += converted.dropped_attributes
        tally.note_problems(converted.problems)
        if converted.result is None:
            tally.errors += 1
            continue
        if dry_run:
            tally.imported += 1
            continue

        if record.meta:
            # e.g. CVAT video track exports carry frame_count/fps (EXP-6).
            item.meta = {**item.meta, **record.meta}

        try:
            await create_version(
                session,
                item=item,
                result=converted.result,
                label_schema_version_id=schema_version.id,
                author_user_id=author_id,
                status=status,
            )
        except ValidationFailedError as exc:
            # QA-6 rules (required attributes, tool not allowed for the class):
            # a problem of this record, not of the import.
            violations = exc.extra.get("violations") if exc.extra else None
            first = violations[0] if isinstance(violations, list) and violations else exc.detail
            tally.note_problems([f"{record.path}: {first}"])
            tally.errors += 1
            continue
        tally.imported += 1

        if status is AnnotationStatus.SUBMITTED and item.status in _IMPORT_ADVANCES:
            # Bulk system action, not a user transition: the workflow table has
            # no `new → submitted` edge because a human never takes it.
            item.status = ItemStatus.SUBMITTED

        if position % _IMPORT_COMMIT_EVERY == 0:
            await session.commit()
            await report(20 + min(79, int(position / total * 79)))

    if not dry_run:
        await session.commit()
    return tally.as_result(import_format=import_format, dry_run=dry_run)


async def import_annotations(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Read a dataset in a foreign format and write one annotation version per matched item.

    Payload: `{"format", "connector_id"?, "path", "class_mapping"?, "status"?,
    "dry_run"?, "label_schema_version_id"?, "created_by_id"}`. See CONTRACTS
    `import` for matching, class mapping and status rules. Not idempotent: a
    re-run adds another version to every matched item; `dry_run` previews.
    """
    return await run_job(ctx, job_id, _import_annotations)


def _not_implemented(what: str) -> JobWork:
    async def work(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
        raise NotImplementedError(f"{what} is not implemented yet")

    return work


async def _tile_image(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
    payload = job.payload
    settings = get_settings()
    project = await _project(session, job)
    cache_connector = await _connector(session, project.effective_cache_connector_id, "cache")
    raw_ids = payload.get("item_ids")
    item_ids = [UUID(str(value)) for value in raw_ids] if isinstance(raw_ids, list) else None
    items = await tiling.select_items(
        session, project.id, force=bool(payload.get("force")), item_ids=item_ids
    )

    tiled = 0
    skipped_small = 0
    skipped_too_large = 0
    errors: list[str] = []
    await report(1)
    if not items:
        return {
            "selected": 0,
            "tiled": 0,
            "skipped_small": 0,
            "skipped_too_large": 0,
            "errors": errors,
        }

    source_connectors: dict[UUID, Connector] = {}
    for item in items:
        if item.connector_id not in source_connectors:
            source_connectors[item.connector_id] = await _connector(
                session, item.connector_id, "source"
            )

    async with storage_for(cache_connector) as cache_storage:
        for connector_id, connector in source_connectors.items():
            async with storage_for(connector) as source_storage:
                for index, item in enumerate(items, start=1):
                    if item.connector_id != connector_id:
                        continue
                    try:
                        outcome = await tiling.tile_item(
                            source_storage=source_storage,
                            cache_storage=cache_storage,
                            item_id=item.id,
                            item_path=item.path,
                            item_size_bytes=item.size_bytes,
                            work_dir=settings.tile_work_dir,
                            min_pixels=settings.tile_min_pixels,
                            max_source_bytes=settings.tile_max_source_bytes,
                            thumbnail_size=settings.thumbnail_size,
                        )
                    except (tiling.TilingError, ConnectorError) as exc:
                        errors.append(f"{item.path}: {exc}")
                        continue

                    if outcome.status == "skipped_too_large":
                        skipped_too_large += 1
                        continue
                    if outcome.status == "skipped_small":
                        skipped_small += 1
                        continue

                    assert outcome.meta is not None and outcome.thumbnail is not None
                    item.meta = {**item.meta, "tiles": outcome.meta}
                    if item.width is None:
                        item.width = outcome.width
                    if item.height is None:
                        item.height = outcome.height
                    thumb_path = thumbnail_blob_path(item.id)
                    await cache_storage.write(thumb_path, outcome.thumbnail, THUMBNAIL_CONTENT_TYPE)
                    item.thumbnail_path = thumb_path
                    tiled += 1
                    await session.commit()
                    await report(int(index / len(items) * 100))
        await session.commit()
    return {
        "selected": len(items),
        "tiled": tiled,
        "skipped_small": skipped_small,
        "skipped_too_large": skipped_too_large,
        "errors": errors,
    }


async def tile_image(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Build a Deep Zoom tile pyramid for large image items (IMG-1, IMG-2).

    Payload: `{"item_ids"?: [uuid], "force"?: bool}`. See CONTRACTS
    `tile_image` for the grid math, upload layout and the `skipped_small` /
    `skipped_too_large` / `errors` counters. Whole-slide formats libvips
    cannot open (`.svs`, `.ndpi`, `.mrxs`) fail per item, counted in `errors`.
    """
    return await run_job(ctx, job_id, _tile_image)


# --------------------------------------------------------------------------- #
# job_type: rebuild_cache (SRC-6)
# --------------------------------------------------------------------------- #


def _rebuild_cache(queue: JobQueue) -> JobWork:
    async def work(session: AsyncSession, job: Job, report: ProgressReporter) -> dict[str, Any]:
        project = await _project(session, job)
        connector = await _connector(session, project.effective_cache_connector_id, "cache")
        purged = 0
        purge_errors: list[str] = []
        await report(1)
        if job.payload.get("purge"):
            item_ids = (
                await session.scalars(
                    select(Item.id)
                    .where(
                        Item.project_id == project.id,
                        Item.media_type.in_((MediaType.IMAGE, MediaType.PDF)),
                    )
                    .order_by(Item.created_at, Item.id)
                )
            ).all()
            async with storage_for(connector) as storage:
                for index, item_id in enumerate(item_ids, start=1):
                    deleted, errors = await derived_cache.purge_item_cache(storage, item_id)
                    purged += deleted
                    purge_errors.extend(errors)
                    if index % _THUMBNAIL_BATCH_SIZE == 0:
                        await report(int(index / len(item_ids) * 90))

        # Pointers and follow-up rows commit together: a retry after a lost
        # enqueue finds the rows `queued` (the stranded-job cron re-enqueues
        # them) instead of pointers already cleared and the tiled ids gone.
        cleared = await derived_cache.clear_project_cache(session, project.id)
        follow_ups = [Job(project_id=project.id, type=JobType.THUMBNAIL, payload={})]
        if cleared.tiled_item_ids:
            follow_ups.append(
                Job(
                    project_id=project.id,
                    type=JobType.TILE_IMAGE,
                    payload={"item_ids": [str(item_id) for item_id in cleared.tiled_item_ids]},
                )
            )
        for follow_up in follow_ups:
            follow_up.status = JobStatus.QUEUED
            session.add(follow_up)
        await session.commit()
        for follow_up in follow_ups:
            try:
                await queue.enqueue(follow_up)
            except QueueUnavailableError as exc:
                log.warning(
                    "rebuild_cache.enqueue_deferred", job_id=str(follow_up.id), error=str(exc)
                )
        return {
            "cleared_thumbnails": cleared.thumbnails,
            "cleared_tiles": len(cleared.tiled_item_ids),
            "purged_blobs": purged,
            "purge_errors": purge_errors,
            "thumbnail_job_id": str(follow_ups[0].id),
            "tile_job_id": str(follow_ups[1].id) if len(follow_ups) > 1 else None,
        }

    return work


async def rebuild_cache(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Forget and regenerate the project's derived data (SRC-6).

    Payload: `{"purge"?: bool, "reason"?: str}`. Clears every item's
    `thumbnail_path` and `meta.tiles`, optionally deleting the old blobs
    first (per item: the container may be shared), then queues a `thumbnail`
    job and a `tile_image` job for the items that had tiles.
    """
    queue = _queue_from_ctx(ctx)
    if queue is None:
        raise LookupError("rebuild_cache needs a job queue to chain its follow-up jobs")
    return await run_job(ctx, job_id, _rebuild_cache(queue))
