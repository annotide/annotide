"""Project and label-schema-version endpoints (§11 REST API).

A project is the top-level unit of work: it owns items, tasks, annotations
and (through ``label_schema_id``) the label schema its annotators draw
against. This router only wires HTTP to the service-layer helpers; workflow
and validation rules live elsewhere (``docs/CONTRACTS.md``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientIpDep,
    CurrentUserDep,
    IdempotencyKeyDep,
    LicenceDep,
    PageParamsDep,
    QueueFactoryDep,
    SessionDep,
)
from app.api.errors import ForbiddenError, LicenceFeatureError, ServiceUnavailableError
from app.core.logging import get_logger
from app.models import JobType, LabelSchema, LabelSchemaVersion, Membership, Project, ProjectRole
from app.schemas import (
    BaseSchema,
    LabelSchemaDefinition,
    Page,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
)
from app.services import audit, idempotency
from app.services.jobs import AuditActor, submit_job
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.features import refusal_message as feature_refusal
from app.services.licensing.state import EffectiveLicense
from app.services.repository import ensure_project_member, get_or_404, paginate
from app.services.storage import check_pdf_mode, check_project_connectors

router = APIRouter(prefix="/projects", tags=["projects"])
log = get_logger(__name__)

#: `idempotency_key.endpoint` for project creation (API-2).
_CREATE_ENDPOINT = "project.create"


class LabelSchemaVersionRead(BaseSchema):
    """One immutable version of a project's label schema (TOOL-4)."""

    id: UUID
    label_schema_id: UUID
    version: int
    definition: dict[str, Any]
    created_at: datetime


async def _find_by_idempotency_key(
    session: AsyncSession, organization_id: UUID, key: str
) -> Project | None:
    """The project an earlier create in `organization_id` made with this key."""
    project_id = await idempotency.find(
        session, organization_id=organization_id, endpoint=_CREATE_ENDPOINT, key=key
    )
    if project_id is None:
        return None
    return await session.get(Project, project_id)


@router.get(
    "",
    response_model=Page[ProjectRead],
    summary="List projects in the caller's organization",
)
async def list_projects(
    session: SessionDep,
    current_user: CurrentUserDep,
    page_params: PageParamsDep,
) -> Page[ProjectRead]:
    """List projects, newest first: the caller's memberships, or all for a superuser.

    Every other project route refuses a non-member (`ensure_project_member`),
    so the list must not reveal those projects' names or settings either.
    """
    stmt = select(Project).where(Project.organization_id == current_user.organization_id)
    if not current_user.is_superuser:
        member_of = select(Membership.project_id).where(Membership.user_id == current_user.id)
        stmt = stmt.where(Project.id.in_(member_of))
    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(Project.created_at, Project.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[ProjectRead](
        items=[ProjectRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create_project(
    payload: ProjectCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
    client_ip: ClientIpDep,
    licence: LicenceDep,
) -> ProjectRead:
    """Create a project.

    Honours the `Idempotency-Key` header (API-2): if a project in this
    organization was already created with that key, it is returned as-is
    with 200 rather than creating a duplicate — also when two retries race,
    in which case the loser's commit fails on the key's unique index and it
    answers with the winner's project.
    """
    if idempotency_key:
        existing = await _find_by_idempotency_key(
            session, current_user.organization_id, idempotency_key
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return ProjectRead.model_validate(existing)

    await check_project_connectors(
        session,
        current_user.organization_id,
        source_connector_id=payload.source_connector_id,
        result_connector_id=payload.result_connector_id,
        cache_connector_id=payload.cache_connector_id,
    )
    check_pdf_mode(payload.settings, payload.result_connector_id)
    project = Project(
        organization_id=current_user.organization_id,
        name=payload.name,
        description=payload.description,
        label_schema_id=payload.label_schema_id,
        source_connector_id=payload.source_connector_id,
        result_connector_id=payload.result_connector_id,
        cache_connector_id=payload.cache_connector_id,
        source_prefix=payload.source_prefix,
        source_glob=payload.source_glob,
        workflow=_licensed_workflow(payload.workflow.model_dump(mode="json"), {}, licence),
        settings=dict(payload.settings),
    )
    session.add(project)
    await session.flush()
    if idempotency_key:
        idempotency.remember(
            session,
            organization_id=current_user.organization_id,
            endpoint=_CREATE_ENDPOINT,
            key=idempotency_key,
            target_id=project.id,
        )
    # The creator owns the project: every later write on it is owner-gated
    # (`ensure_project_member`), so without this row the creator could never
    # edit, delete or add members to their own project.
    session.add(Membership(user_id=current_user.id, project_id=project.id, role=ProjectRole.OWNER))
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="project.create",
        target_type="project",
        target_id=project.id,
        after={"name": project.name},
        ip=client_ip,
    )
    try:
        await session.commit()
    except IntegrityError:
        # Lost the race on the idempotency key: answer with the winner's row.
        await session.rollback()
        if idempotency_key:
            existing = await _find_by_idempotency_key(
                session, current_user.organization_id, idempotency_key
            )
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return ProjectRead.model_validate(existing)
        raise
    await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Get a project by id",
)
async def get_project(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> ProjectRead:
    """Return one project. The caller must be a member of it."""
    await ensure_project_member(session, project_id, current_user)
    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    return ProjectRead.model_validate(project)


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="Update a project (owner only)",
)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    session: SessionDep,
    queue_factory: QueueFactoryDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    response: Response,
    licence: LicenceDep,
) -> ProjectRead:
    """Partially update a project.

    Uses `exclude_unset` so a field the client omits is left untouched, while
    a field explicitly sent as `null` clears it. Requires the `owner` role.

    Changing where `cache/` lives (the cache connector, or the result
    connector while no cache connector is set) queues a `rebuild_cache` job,
    since existing thumbnails and tiles point into the old container (SRC-6);
    its id comes back in `X-Rebuild-Job-Id`. The update stands when the queue
    is unreachable: the header is then absent, and the rebuild can be queued
    later with `POST /projects/{id}/cache/rebuild`.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may update this project.")

    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    changes = payload.model_dump(exclude_unset=True)
    await check_project_connectors(
        session,
        current_user.organization_id,
        source_connector_id=changes.get("source_connector_id"),
        result_connector_id=changes.get("result_connector_id"),
        cache_connector_id=changes.get("cache_connector_id"),
    )
    check_pdf_mode(
        changes.get("settings", project.settings),
        changes.get("result_connector_id", project.result_connector_id),
    )
    cache_before = project.effective_cache_connector_id
    if payload.workflow is not None:
        # Plain JSON in the JSONB column, not enum members (WF-1).
        changes["workflow"] = _licensed_workflow(
            payload.workflow.model_dump(mode="json"), project.workflow or {}, licence
        )
    before = {field: jsonable_encoder(getattr(project, field)) for field in changes}
    for field, value in changes.items():
        setattr(project, field, value)

    if changes:
        audit.record(
            session,
            organization_id=current_user.organization_id,
            actor_id=current_user.id,
            action="project.update",
            target_type="project",
            target_id=project.id,
            before=before,
            after=jsonable_encoder(changes),
            ip=client_ip,
        )

    await session.commit()
    await session.refresh(project)
    cache_after = project.effective_cache_connector_id
    if cache_after is not None and cache_after != cache_before:
        try:
            job = await submit_job(
                session,
                await queue_factory(),
                project_id=project.id,
                job_type=JobType.REBUILD_CACHE,
                payload={"reason": "cache_connector_changed"},
                actor=AuditActor(current_user.organization_id, current_user.id, client_ip),
            )
        except ServiceUnavailableError as exc:
            log.warning(
                "project.cache_rebuild_not_queued", project_id=str(project.id), error=str(exc)
            )
        else:
            response.headers["X-Rebuild-Job-Id"] = str(job.id)
        await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.delete(
    "/{project_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project (owner only)",
)
async def delete_project(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> None:
    """Delete a project.

    `Project` has no `deleted_at` column, so this is a hard delete; dependent
    rows are removed by the database's own `ON DELETE CASCADE`. Requires the
    `owner` role.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may delete this project.")

    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="project.delete",
        target_type="project",
        target_id=project.id,
        before={"name": project.name},
        ip=client_ip,
    )
    await session.delete(project)
    await session.commit()


@router.get(
    "/{project_id}/schemas",
    response_model=list[LabelSchemaVersionRead],
    summary="List a project's label schema versions, newest first",
)
async def list_schema_versions(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> list[LabelSchemaVersion]:
    """Return every version of the project's label schema, newest version first."""
    await ensure_project_member(session, project_id, current_user)

    rows = await session.scalars(
        select(LabelSchemaVersion)
        .join(LabelSchema, LabelSchema.id == LabelSchemaVersion.label_schema_id)
        .where(LabelSchema.project_id == project_id)
        .order_by(LabelSchemaVersion.version.desc())
    )
    return list(rows)


@router.post(
    "/{project_id}/schemas",
    response_model=LabelSchemaVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create the next label schema version for a project (owner only)",
)
async def create_schema_version(
    project_id: UUID,
    payload: LabelSchemaDefinition,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> LabelSchemaVersion:
    """Create a new label schema version.

    Versions are immutable (TOOL-4): an existing one is never updated. The
    new version number is `max(version) + 1` for the project's label schema,
    computed in SQL. The posted body is validated as a `LabelSchemaDefinition`
    before it is stored.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may add a label schema version.")

    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )

    if project.label_schema_id is None:
        label_schema = LabelSchema(project_id=project.id, name=f"{project.name} schema")
        session.add(label_schema)
        await session.flush()
        project.label_schema_id = label_schema.id
        label_schema_id = label_schema.id
    else:
        label_schema_id = project.label_schema_id

    next_version = await session.scalar(
        select(func.coalesce(func.max(LabelSchemaVersion.version), 0) + 1).where(
            LabelSchemaVersion.label_schema_id == label_schema_id
        )
    )

    version = LabelSchemaVersion(
        label_schema_id=label_schema_id,
        version=next_version,
        definition=payload.model_dump(mode="json"),
    )
    session.add(version)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="label_schema.create",
        target_type="label_schema_version",
        target_id=version.id,
        after={"version": version.version},
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(version)
    return version


def _quality_settings(workflow: dict[str, Any]) -> tuple[int, Any]:
    """The workflow fields that switch on consensus (QA-1) and gold tasks (QA-4)."""
    return int(workflow.get("consensus_annotators") or 1), workflow.get("gold_every")


def _licensed_workflow(
    new: dict[str, Any], old: dict[str, Any], licence: EffectiveLicense
) -> dict[str, Any]:
    """Refuse turning consensus or gold on, or changing them, without `quality` (LIC-33).

    Turning them off, or leaving them as they are, never needs the licence.
    """
    consensus, gold = _quality_settings(new)
    quality_on = consensus > 1 or gold is not None
    if (
        quality_on
        and _quality_settings(new) != _quality_settings(old)
        and not has_feature(licence, Feature.QUALITY)
    ):
        raise LicenceFeatureError(feature_refusal(Feature.QUALITY))
    return new
