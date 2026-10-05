"""Background-job endpoints: status lookup, the queue-a-job actions, cancel and retry.

`§11 REST API` puts these under two different path prefixes (`/jobs/...` and
`/projects/{id}/...`), so this router carries no shared `prefix` of its own.
Queueing goes through `app.services.jobs.submit_job`: the row is committed
first, then handed to arq (ARC-4).
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select

from app.api.deps import (
    UNRESTRICTED_LICENCE,
    ClientIpDep,
    CurrentUser,
    CurrentUserDep,
    IdempotencyKeyDep,
    PageParamsDep,
    QueueDep,
    SessionDep,
    SettingsDep,
)
from app.api.errors import (
    ConflictError,
    ForbiddenError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from app.connectors.errors import ConnectorError
from app.importers import IMPORTERS, get_importer
from app.models import Connector, Job, JobStatus, JobType, Model, ModelVersion, Project
from app.schemas import BaseSchema, ImportRequest, ImportStatus, JobRead, Page, PrelabelRequest
from app.schemas.job import CacheRebuildRequest, ExtractTextRequest
from app.schemas.tiles import TileJobRequest
from app.services import datasets
from app.services.datasets import DatasetFilter, SplitName
from app.services.importing import MAX_IMPORT_BYTES
from app.services.jobs import AuditActor, cancel_job, find_replayed_job, retry_job, submit_job
from app.services.repository import ensure_project_member, get_or_404, member_prefixes, paginate
from app.services.secrets import SecretResolutionError
from app.services.storage import storage_for

router = APIRouter(tags=["jobs"])


async def _replayed(
    session: Any,
    current_user: CurrentUser,
    job_type: JobType,
    idempotency_key: str | None,
    response: Response,
) -> Job | None:
    """The job an earlier create with this `Idempotency-Key` made (API-2), as a 200."""
    if not idempotency_key:
        return None
    existing = await find_replayed_job(
        session,
        organization_id=current_user.organization_id,
        job_type=job_type,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
    return existing


class ExportRequest(BaseSchema):
    """Body for queuing an export job (EXP-5).

    Either `snapshot_id` (export exactly that frozen set) or `filter` (export
    the latest annotation of every matching item right now). `split` picks
    one partition of a split snapshot (EXP-3); it needs `snapshot_id`.
    """

    format: str
    snapshot_id: UUID | None = None
    filter: DatasetFilter = Field(default_factory=DatasetFilter)
    label_schema_version_id: UUID | None = None
    split: SplitName | None = None


class ScanRequest(BaseSchema):
    """Body for queuing a source-scan job (SRC-2).

    Both fields are optional overrides of the project's own `source_prefix`
    / `source_glob`; an empty body scans with the project's defaults.
    """

    prefix: str | None = None
    glob: str | None = None


class ExportDownload(BaseSchema):
    """A short-lived signed URL for a succeeded export's archive (EXP-5)."""

    url: str
    expires_in: int


async def _visible_job(session: Any, job_id: UUID, current_user: CurrentUser) -> Job:
    """Load a job the caller may see: through project membership, or as a superuser.

    A job with no project (none of the job types this API creates leave it
    unset, but the column is nullable) is visible only to a superuser, since
    there is no project membership to check it against.
    """
    job = await get_or_404(session, Job, job_id)
    if job.project_id is None:
        if not current_user.is_superuser:
            raise ForbiddenError("This job has no project, so only administrators may view it.")
    else:
        await ensure_project_member(session, job.project_id, current_user)
    return job


@router.get(
    "/jobs/{job_id}",
    response_model=JobRead,
    summary="Get a job's status and progress",
)
async def get_job(
    job_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> Job:
    """Return one job. Authorised through the job's project."""
    return await _visible_job(session, job_id, current_user)


@router.get(
    "/jobs/{job_id}/download",
    response_model=ExportDownload,
    summary="Signed download URL for a finished export (EXP-5)",
)
async def download_export(
    job_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> ExportDownload:
    """Mint a signed URL for a succeeded export's archive.

    The API never proxies media (including export archives): it only signs a
    URL against the project's result connector and hands it back, capped at
    `settings.signed_url_ttl`, exactly like item media URLs (AUTH-6).
    """
    job = await _visible_job(session, job_id, current_user)

    blob_path = (job.result or {}).get("blob_path")
    if (
        job.type != JobType.EXPORT
        or job.status != JobStatus.SUCCEEDED
        or not isinstance(blob_path, str)
        or not blob_path
        or job.project_id is None
    ):
        raise ConflictError("This job is not a succeeded export with an archive to download.")

    # An export covers the whole project; a folder-limited member (§4) may
    # not take what their folders do not hold.
    if await member_prefixes(session, job.project_id, current_user) is not None:
        raise ForbiddenError(
            "An export covers the whole project; your access is limited to folders."
        )

    project = await get_or_404(session, Project, job.project_id)
    if project.result_connector_id is None:
        raise ConflictError("This project has no result connector configured.")
    connector = await get_or_404(session, Connector, project.result_connector_id)

    try:
        async with storage_for(connector) as storage:
            url = await storage.signed_url(blob_path, expires_in=settings.signed_url_ttl)
    except (ConnectorError, SecretResolutionError) as exc:
        # Neither is the caller's fault, but both mean the archive cannot be
        # reached right now, which is a state conflict rather than a 500: the
        # job succeeded, yet its result connector is unusable.
        raise ConflictError(
            "The export's result connector could not be reached to sign a download URL."
        ) from exc

    return ExportDownload(url=url, expires_in=settings.signed_url_ttl)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobRead,
    summary="Cancel a queued or running job (ARC-4)",
)
async def cancel(
    job_id: UUID,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
) -> Job:
    """Mark the job cancelled and ask the worker to stop it. 409 if it already finished."""
    job = await _visible_job(session, job_id, current_user)
    return await cancel_job(session, queue, job)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-queue a failed or cancelled job (ARC-4)",
)
async def retry(
    job_id: UUID,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
) -> Job:
    """Re-queue the same job with the same payload. 409 unless it failed or was cancelled."""
    job = await _visible_job(session, job_id, current_user)
    return await retry_job(session, queue, job)


@router.get(
    "/projects/{project_id}/jobs",
    response_model=Page[JobRead],
    summary="List jobs for a project",
)
async def list_project_jobs(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page_params: PageParamsDep,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    job_type: Annotated[JobType | None, Query(alias="type")] = None,
) -> Page[JobRead]:
    """List jobs for a project, optionally filtered by `status` and/or `type`."""
    await ensure_project_member(session, project_id, current_user)

    stmt = select(Job).where(Job.project_id == project_id)
    if job_status is not None:
        stmt = stmt.where(Job.status == job_status)
    if job_type is not None:
        stmt = stmt.where(Job.type == job_type)

    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(Job.created_at, Job.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[JobRead](
        items=[JobRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "/projects/{project_id}/exports",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an export job (EXP-5)",
)
async def create_export_job(
    project_id: UUID,
    payload: ExportRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue an export job. The archive lands at `exports/{job_id}/{format}.zip`.

    `format` is validated against the registered exporters via `services/datasets.py`.
    """
    await ensure_project_member(session, project_id, current_user)

    datasets.require_export_format(payload.format)

    job_payload: dict[str, Any] = {
        "format": payload.format,
        "filter": payload.filter.model_dump(mode="json", exclude_none=True),
    }
    if payload.snapshot_id is not None:
        job_payload["snapshot_id"] = str(payload.snapshot_id)
    if payload.label_schema_version_id is not None:
        job_payload["label_schema_version_id"] = str(payload.label_schema_version_id)
    if payload.split is not None:
        if payload.snapshot_id is None:
            raise ValidationFailedError("`split` requires a `snapshot_id`.")
        job_payload["split"] = payload.split

    replayed = await _replayed(session, current_user, JobType.EXPORT, idempotency_key, response)
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.EXPORT,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


def _validate_import_format(import_format: str) -> None:
    try:
        get_importer(import_format)
    except KeyError as exc:
        known = ", ".join(sorted(IMPORTERS))
        raise ValidationFailedError(
            f"Unknown import format '{import_format}'. Known formats: {known}."
        ) from exc


_ATTRIBUTE_MAPPING = TypeAdapter(dict[str, dict[str, str | None]])


def _json_form_object(name: str, raw: str | None) -> dict[str, Any]:
    """Parse a JSON-object form field; an absent or empty field is `{}`."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError(f"{name} must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailedError(f"{name} must be a JSON object.")
    return parsed


async def _queue_import(
    session: Any,
    queue: Any,
    *,
    project_id: UUID,
    request: ImportRequest,
    current_user: CurrentUser,
    client_ip: str | None,
    idempotency_key: str | None = None,
) -> Job:
    job_payload: dict[str, Any] = {
        "format": request.format,
        "path": request.path,
        "class_mapping": request.class_mapping,
        "status": request.status.value,
        "dry_run": request.dry_run,
        "created_by_id": str(current_user.id),
    }
    if request.attribute_mapping:
        job_payload["attribute_mapping"] = request.attribute_mapping
    if request.connector_id is not None:
        job_payload["connector_id"] = str(request.connector_id)
    if request.label_schema_version_id is not None:
        job_payload["label_schema_version_id"] = str(request.label_schema_version_id)
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.IMPORT,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/imports",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an import job from a file already on a connector (EXP-6)",
)
async def create_import_job(
    project_id: UUID,
    payload: ImportRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue an import of `path` on `connector_id` (default: the source connector).

    Owner only, like pre-labelling: an import writes annotation versions
    across the project. `format` is validated against the registered
    importers (`app.importers`); `dry_run` is the preview to run first.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may queue an import.")
    _validate_import_format(payload.format)
    if payload.connector_id is not None:
        await get_or_404(
            session, Connector, payload.connector_id, organization_id=current_user.organization_id
        )
    replayed = await _replayed(session, current_user, JobType.IMPORT, idempotency_key, response)
    if replayed is not None:
        return replayed
    return await _queue_import(
        session,
        queue,
        project_id=project_id,
        request=payload,
        current_user=current_user,
        client_ip=client_ip,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/imports/upload",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an annotation file or archive and queue its import (EXP-6)",
)
async def upload_import_job(
    project_id: UUID,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    file: Annotated[UploadFile, File()],
    format: Annotated[str, Form()],  # noqa: A002 - matches the contract's field name
    status_: Annotated[ImportStatus, Form(alias="status")] = ImportStatus.SUBMITTED,
    dry_run: Annotated[bool, Form()] = False,
    class_mapping: Annotated[str | None, Form()] = None,
    attribute_mapping: Annotated[str | None, Form()] = None,
    label_schema_version_id: Annotated[UUID | None, Form()] = None,
) -> Job:
    """Store the upload at `imports/{upload_id}/{filename}` on the result connector, then queue.

    The upload is annotation data, not media, and it is bounded
    (`MAX_IMPORT_BYTES`); the job reads it back from the connector like any
    other input, so the API holds it only for the duration of this request.
    `class_mapping` and `attribute_mapping` are JSON objects in form fields.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may queue an import.")
    _validate_import_format(format)

    mapping = {str(k): str(v) for k, v in _json_form_object("class_mapping", class_mapping).items()}
    try:
        attributes = _ATTRIBUTE_MAPPING.validate_python(
            _json_form_object("attribute_mapping", attribute_mapping)
        )
    except ValidationError as exc:
        raise ValidationFailedError(
            "attribute_mapping must map class names to {source: target or null} objects."
        ) from exc

    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    if project.result_connector_id is None:
        raise ConflictError("The project has no result connector to store the upload on.")
    connector = await get_or_404(session, Connector, project.result_connector_id)

    data = await file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise PayloadTooLargeError(f"Import files are limited to {MAX_IMPORT_BYTES} bytes.")
    if not data:
        raise ValidationFailedError("The uploaded file is empty.")
    filename = PurePosixPath(file.filename or "import.bin").name or "import.bin"
    blob_path = f"imports/{uuid4()}/{filename}"
    try:
        async with storage_for(connector) as storage:
            await storage.write(blob_path, data, file.content_type or "application/octet-stream")
    except (ConnectorError, SecretResolutionError) as exc:
        raise ConflictError(f"Could not store the upload on the result connector: {exc}") from exc

    request = ImportRequest(
        format=format,
        path=blob_path,
        connector_id=connector.id,
        class_mapping=mapping,
        attribute_mapping=attributes,
        status=status_,
        dry_run=dry_run,
        label_schema_version_id=label_schema_version_id,
    )
    return await _queue_import(
        session,
        queue,
        project_id=project_id,
        request=request,
        current_user=current_user,
        client_ip=client_ip,
    )


@router.post(
    "/projects/{project_id}/scan",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a source-scan job (SRC-2)",
)
async def create_scan_job(
    project_id: UUID,
    payload: ScanRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue a job that scans the project's source connector for new items."""
    await ensure_project_member(session, project_id, current_user)

    job_payload: dict[str, Any] = {}
    if payload.prefix is not None:
        job_payload["prefix"] = payload.prefix
    if payload.glob is not None:
        job_payload["glob"] = payload.glob

    replayed = await _replayed(
        session, current_user, JobType.SCAN_SOURCE, idempotency_key, response
    )
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.SCAN_SOURCE,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


class ThumbnailRequest(BaseSchema):
    """Body for `POST /projects/{id}/thumbnails` (IMG-8)."""

    #: Regenerate thumbnails that already exist.
    force: bool = False
    #: Restrict the run to these items; default is every image item.
    item_ids: list[UUID] | None = Field(default=None, max_length=10_000)


@router.post(
    "/projects/{project_id}/tiles",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a large-image tiling job (IMG-1)",
)
async def create_tile_job(
    project_id: UUID,
    payload: TileJobRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue a job that builds a DZI tile pyramid for the project's large images.

    Owner or reviewer, like other actions that write derived data across the
    whole project (`assign` / `approve` on bulk, quality management). Scans
    queue this on their own for a new large image; this endpoint exists for
    projects scanned before tiling existed and for `force` regeneration.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role not in ("owner", "reviewer"):
        raise ForbiddenError("Only a project owner or reviewer may queue tiling.")

    job_payload: dict[str, Any] = {}
    if payload.force:
        job_payload["force"] = True
    if payload.item_ids is not None:
        job_payload["item_ids"] = [str(item_id) for item_id in payload.item_ids]

    replayed = await _replayed(session, current_user, JobType.TILE_IMAGE, idempotency_key, response)
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.TILE_IMAGE,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/thumbnails",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a thumbnail-generation job (IMG-8)",
)
async def create_thumbnail_job(
    project_id: UUID,
    payload: ThumbnailRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue a job that writes `cache/thumbnails/{item_id}.jpg` for the project's images.

    Scans queue this on their own for new items; this endpoint exists for
    projects scanned before thumbnails existed and for regeneration after
    `APP_THUMBNAIL_SIZE` changes (`force`).
    """
    await ensure_project_member(session, project_id, current_user)

    job_payload: dict[str, Any] = {}
    if payload.force:
        job_payload["force"] = True
    if payload.item_ids is not None:
        job_payload["item_ids"] = [str(item_id) for item_id in payload.item_ids]

    replayed = await _replayed(session, current_user, JobType.THUMBNAIL, idempotency_key, response)
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.THUMBNAIL,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/cache/rebuild",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a derived-data cache rebuild (SRC-6)",
)
async def create_cache_rebuild_job(
    project_id: UUID,
    payload: CacheRebuildRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue a job that forgets and regenerates the project's thumbnails and tiles.

    Owner only: every item's thumbnail disappears from the grid until the
    chained `thumbnail` job writes it again. `purge` also deletes the old
    blobs from the cache connector first.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may rebuild the cache.")
    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    if project.effective_cache_connector_id is None:
        raise ConflictError("The project has no cache or result connector to rebuild.")

    replayed = await _replayed(
        session, current_user, JobType.REBUILD_CACHE, idempotency_key, response
    )
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.REBUILD_CACHE,
        payload={"purge": True} if payload.purge else {},
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/extract-text",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue PDF text extraction (PDF text mode)",
)
async def create_extract_text_job(
    project_id: UUID,
    payload: ExtractTextRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue an `extract_text` job: retries PDF texts that are pending or failed,
    and with `force` re-extracts ready ones nobody has annotated.

    Owner only. 409 when the project has no result connector to write to.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may extract PDF text.")
    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    if project.result_connector_id is None:
        raise ConflictError("The project has no result connector to write the text to.")

    replayed = await _replayed(
        session, current_user, JobType.EXTRACT_TEXT, idempotency_key, response
    )
    if replayed is not None:
        return replayed
    job_payload: dict[str, Any] = {"force": True} if payload.force else {}
    if payload.item_ids is not None:
        job_payload["item_ids"] = [str(item_id) for item_id in payload.item_ids]
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.EXTRACT_TEXT,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/projects/{project_id}/prelabel",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a pre-labelling job (ML-2)",
)
async def create_prelabel_job(
    project_id: UUID,
    payload: PrelabelRequest,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Queue a job that runs a model version over the project's items (ML-2, ML-4, ML-10).

    Restricted to the `owner` role: pre-labelling writes draft annotations
    across the project, unlike the read-mostly endpoints any member can use.
    `limit` is the dry run a customer runs before committing to a full pass
    (BYOM-7).
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may queue pre-labelling.")

    version = await get_or_404(session, ModelVersion, payload.model_version_id)
    model = await get_or_404(
        session, Model, version.model_id, organization_id=current_user.organization_id
    )
    if not model.endpoint_url:
        raise ConflictError(
            "This model has no endpoint: it is an external producer that posts its own "
            "pre-labels (POST /items/{id}/prelabels)."
        )

    job_payload: dict[str, Any] = {
        "model_version_id": str(payload.model_version_id),
        "filter": payload.filter.model_dump(mode="json", exclude_none=True),
        "confidence_threshold": payload.confidence_threshold,
    }
    if payload.label_schema_version_id is not None:
        job_payload["label_schema_version_id"] = str(payload.label_schema_version_id)
    if payload.limit is not None:
        job_payload["limit"] = payload.limit
    if payload.prioritize_uncertain:
        job_payload["prioritize_uncertain"] = True

    replayed = await _replayed(session, current_user, JobType.PRELABEL, idempotency_key, response)
    if replayed is not None:
        return replayed
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.PRELABEL,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )
