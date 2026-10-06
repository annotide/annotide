"""Snapshot endpoints (EXP-1, EXP-2): freeze a dataset, list what was frozen.

A snapshot is produced by a background job, not inline: freezing a large
project means reading every latest annotation and writing two blobs, which
is too slow for a request. `POST` therefore returns the *job* (202); the
`snapshot` row appears when the job succeeds, and `job.result.snapshot_id`
names it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import ValidationError
from sqlalchemy import func, or_, select

from app.api.deps import (
    ClientIpDep,
    CurrentUserDep,
    IdempotencyKeyDep,
    PageParamsDep,
    QueueDep,
    SessionDep,
)
from app.api.errors import ConflictError, NotFoundError, ValidationFailedError
from app.connectors.errors import ConnectorError
from app.models import (
    Annotation,
    Connector,
    Item,
    Job,
    JobType,
    Model,
    ModelVersion,
    Project,
    Snapshot,
)
from app.schemas import (
    JobRead,
    Page,
    SnapshotCreate,
    SnapshotDiff,
    SnapshotLineage,
    SnapshotLineageSnapshot,
    SnapshotLineageVersion,
    SnapshotRead,
)
from app.services.datasets import DatasetFilter, SplitConfig
from app.services.jobs import AuditActor, find_replayed_job, submit_job
from app.services.repository import ensure_project_member, get_or_404, paginate
from app.services.secrets import SecretResolutionError
from app.services.snapshot_diff import diff_snapshots, parse_jsonl
from app.services.storage import storage_for

router = APIRouter(prefix="/projects/{project_id}/snapshots", tags=["snapshots"])


@router.get("", response_model=Page[SnapshotRead], summary="List a project's snapshots")
async def list_snapshots(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page_params: PageParamsDep,
) -> Page[SnapshotRead]:
    await ensure_project_member(session, project_id, current_user)

    result = await paginate(
        session,
        select(Snapshot).where(Snapshot.project_id == project_id),
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(Snapshot.created_at, Snapshot.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[SnapshotRead](
        items=[SnapshotRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.get("/{snapshot_id}", response_model=SnapshotRead, summary="Get one snapshot")
async def get_snapshot(
    project_id: UUID,
    snapshot_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> Snapshot:
    await ensure_project_member(session, project_id, current_user)
    return await _snapshot_or_404(session, project_id, snapshot_id)


async def _snapshot_or_404(session: SessionDep, project_id: UUID, snapshot_id: UUID) -> Snapshot:
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.project_id != project_id:
        raise NotFoundError(f"Snapshot {snapshot_id} does not exist.")
    return snapshot


@router.get(
    "/{snapshot_id}/lineage",
    response_model=SnapshotLineage,
    summary="Model versions trained on a snapshot (EXP-8)",
)
async def snapshot_lineage(
    project_id: UUID,
    snapshot_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> SnapshotLineage:
    """List the model versions that record this snapshot (by id or by the
    same digest) as their training data, oldest first, each with the number
    of this project's items it has written a draft for."""
    await ensure_project_member(session, project_id, current_user)
    snapshot = await _snapshot_or_404(session, project_id, snapshot_id)

    predicted = (
        select(
            Annotation.author_model_version_id.label("version_id"),
            func.count(func.distinct(Annotation.item_id)).label("items_predicted"),
        )
        .join(Item, Item.id == Annotation.item_id)
        .where(Item.project_id == project_id, Annotation.author_model_version_id.is_not(None))
        .group_by(Annotation.author_model_version_id)
        .subquery()
    )
    rows = await session.execute(
        select(ModelVersion, Model.name, func.coalesce(predicted.c.items_predicted, 0))
        .join(Model, Model.id == ModelVersion.model_id)
        .outerjoin(predicted, predicted.c.version_id == ModelVersion.id)
        .where(
            or_(
                ModelVersion.snapshot_id == snapshot.id,
                ModelVersion.snapshot_digest == snapshot.digest,
            ),
            Model.deleted_at.is_(None),
        )
        .order_by(ModelVersion.created_at, ModelVersion.version)
    )
    return SnapshotLineage(
        snapshot=SnapshotLineageSnapshot(
            id=snapshot.id,
            name=snapshot.name,
            digest=snapshot.digest,
            item_count=snapshot.item_count,
            created_at=snapshot.created_at,
        ),
        versions=[
            SnapshotLineageVersion(
                id=version.id,
                model_id=version.model_id,
                model_name=model_name,
                version=version.version,
                snapshot_digest=version.snapshot_digest,
                training_run=version.training_run,
                created_at=version.created_at,
                items_predicted=items_predicted,
            )
            for version, model_name, items_predicted in rows.all()
        ],
    )


@router.get(
    "/{base_id}/diff/{target_id}",
    response_model=SnapshotDiff,
    summary="Compare two snapshots (EXP-4)",
)
async def diff_snapshot(
    project_id: UUID,
    base_id: UUID,
    target_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> SnapshotDiff:
    """What changed from `base` to `target`: items added / removed / changed
    (with shape-level counts per changed item), class balance deltas, and
    train/val/test moves. Reads both snapshots' manifest and JSONL from the
    project's result connector; the live database is not consulted, so the
    answer is exactly about the two frozen sets.
    """
    await ensure_project_member(session, project_id, current_user)
    base = await _snapshot_or_404(session, project_id, base_id)
    target = await _snapshot_or_404(session, project_id, target_id)

    project = await get_or_404(session, Project, project_id)
    if project.result_connector_id is None:
        raise ConflictError("This project has no result connector configured.")
    connector = await get_or_404(session, Connector, project.result_connector_id)

    try:
        async with storage_for(connector) as storage:
            base_manifest = json.loads(await storage.read(f"{base.blob_path}manifest.json"))
            base_documents = parse_jsonl(await storage.read(f"{base.blob_path}annotations.jsonl"))
            target_manifest = json.loads(await storage.read(f"{target.blob_path}manifest.json"))
            target_documents = parse_jsonl(
                await storage.read(f"{target.blob_path}annotations.jsonl")
            )
    except (ConnectorError, SecretResolutionError) as exc:
        raise ConflictError("The snapshots' result connector could not be read.") from exc

    return diff_snapshots(
        base=base,
        base_manifest=base_manifest,
        base_documents=base_documents,
        target=target,
        target_manifest=target_manifest,
        target_documents=target_documents,
    )


@router.post(
    "",
    response_model=JobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a snapshot job (EXP-1)",
)
async def create_snapshot(
    project_id: UUID,
    payload: SnapshotCreate,
    session: SessionDep,
    queue: QueueDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> Job:
    """Freeze the latest annotation of every item matching `filter`.

    Returns the job; poll `GET /jobs/{id}` and read `result.snapshot_id`.
    """
    await ensure_project_member(session, project_id, current_user)

    try:
        dataset_filter = DatasetFilter.model_validate(payload.filter)
    except ValidationError as exc:
        raise ValidationFailedError(
            "The snapshot filter is invalid.",
            extra={"violations": [error["msg"] for error in exc.errors()]},
        ) from exc

    split: SplitConfig | None = None
    if payload.split is not None:
        try:
            split = SplitConfig.model_validate(payload.split)
        except ValidationError as exc:
            raise ValidationFailedError(
                "The snapshot split is invalid.",
                extra={"violations": [error["msg"] for error in exc.errors()]},
            ) from exc

    job_payload: dict[str, Any] = {
        "name": payload.name,
        "filter": dataset_filter.model_dump(mode="json", exclude_none=True),
        "created_by_id": str(current_user.id),
    }
    if split is not None:
        job_payload["split"] = split.model_dump(mode="json")
    if payload.label_schema_version_id is not None:
        job_payload["label_schema_version_id"] = str(payload.label_schema_version_id)

    if idempotency_key:
        existing = await find_replayed_job(
            session,
            organization_id=current_user.organization_id,
            job_type=JobType.SNAPSHOT,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return existing
    return await submit_job(
        session,
        queue,
        project_id=project_id,
        job_type=JobType.SNAPSHOT,
        payload=job_payload,
        actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
        idempotency_key=idempotency_key,
    )
