"""Model registry endpoints (§11 REST API, ML-1, BYOM-2, BYOM-3).

Registering, updating and testing a model endpoint is system administration
(like connectors); listing models and their versions is available to any
member of the organisation so project owners can pick one for prelabeling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientIpDep,
    CurrentUser,
    CurrentUserDep,
    PageParamsDep,
    SessionDep,
    SettingsDep,
    SuperuserDep,
)
from app.api.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import Annotation, Model, ModelVersion, Project, Snapshot
from app.models import ModelTask as DbModelTask
from app.schemas import (
    CorrectionMetrics,
    ModelCheckResult,
    ModelCreate,
    ModelDerivation,
    ModelFamily,
    ModelFamilyVersion,
    ModelIdentity,
    ModelIdentityConfig,
    ModelRead,
    ModelTask,
    ModelUpdate,
    ModelVersionCreate,
    ModelVersionRead,
    Page,
)
from app.services import audit
from app.services.correction_metrics import correction_metrics
from app.services.models import (
    ModelClient,
    ModelRejected,
    ModelUnavailable,
    identity_problem,
    normalise_mapping,
)
from app.services.repository import get_or_404, paginate
from app.services.secrets import SecretResolutionError

router = APIRouter(prefix="/models", tags=["models"])

_CHECK_TIMEOUT_SECONDS = 10


def _to_read(model: Model) -> ModelRead:
    """Build the public representation of a model.

    Never includes `secret_ref`: only whether one is set, via `has_secret`.
    """
    return ModelRead(
        id=model.id,
        organization_id=model.organization_id,
        name=model.name,
        task=model.task,
        endpoint_url=model.endpoint_url,
        identity_type=model.identity_type,
        has_secret=model.secret_ref is not None,
        identity_config=ModelIdentityConfig.model_validate(model.identity_config or {}),
        created_at=model.created_at,
    )


def _audit_after(model: Model) -> dict[str, object]:
    """Audit `after` snapshot: never `secret_ref`, only whether one is set."""
    return {
        "name": model.name,
        "task": model.task.value,
        "endpoint_url": model.endpoint_url,
        "identity_type": model.identity_type,
        "has_secret": model.secret_ref is not None,
        "identity_config": model.identity_config,
    }


def _version_to_read(version: ModelVersion) -> ModelVersionRead:
    return ModelVersionRead(
        id=version.id,
        model_id=version.model_id,
        version=version.version,
        class_mapping=version.class_mapping,
        metrics=version.metrics,
        snapshot_id=version.snapshot_id,
        snapshot_digest=version.snapshot_digest,
        training_run=version.training_run,
        parent_version_id=version.parent_version_id,
        derivation=ModelDerivation(version.derivation) if version.derivation else None,
        created_at=version.created_at,
    )


async def _resolve_lineage(
    session: SessionDep, organization_id: UUID, payload: ModelVersionCreate
) -> tuple[UUID | None, str | None]:
    """Check the snapshot / digest pair a version claims to be trained on (EXP-8).

    Returns the `(snapshot_id, snapshot_digest)` to store: an id fills in the
    row's digest, a digest alone links the organisation's snapshot with that
    content when there is one, and a pair that disagrees is a 409 — a version
    cannot claim data it did not see.
    """
    snapshot_id, digest = payload.snapshot_id, payload.snapshot_digest
    if snapshot_id is None and digest is None:
        return None, None

    in_organization = select(Snapshot).join(Project, Project.id == Snapshot.project_id)
    in_organization = in_organization.where(Project.organization_id == organization_id)
    if snapshot_id is not None:
        snapshot = await session.scalar(in_organization.where(Snapshot.id == snapshot_id))
        if snapshot is None:
            raise NotFoundError(f"Snapshot {snapshot_id} does not exist.")
        if digest is not None and digest != snapshot.digest:
            raise ConflictError(
                f"snapshot_digest {digest} does not match snapshot {snapshot_id} "
                f"(digest {snapshot.digest})."
            )
        return snapshot.id, snapshot.digest

    snapshot = await session.scalar(
        in_organization.where(Snapshot.digest == digest).order_by(Snapshot.created_at).limit(1)
    )
    return (snapshot.id if snapshot is not None else None), digest


@router.get(
    "",
    response_model=Page[ModelRead],
    summary="List models registered in the caller's organization (ML-1)",
)
async def list_models(
    session: SessionDep,
    current_user: CurrentUserDep,
    page_params: PageParamsDep,
) -> Page[ModelRead]:
    """List models, newest first, scoped to the caller's organization."""
    stmt = select(Model).where(
        Model.organization_id == current_user.organization_id, Model.deleted_at.is_(None)
    )
    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(Model.created_at, Model.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[ModelRead](
        items=[_to_read(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "",
    response_model=ModelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a model endpoint (system administrators only, ML-1)",
)
async def create_model(
    payload: ModelCreate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> ModelRead:
    """Register a new model endpoint.

    `secret_ref` is a secret-store reference, never the credential itself
    (SEC/AUTH-7): required for `api_key`, `bearer` and `service_principal`,
    refused for `managed_identity`. Entra identities need `identity_config`.
    """
    identity_config = payload.identity_config.model_dump(exclude_none=True)
    problem = identity_problem(payload.identity_type.value, payload.secret_ref, identity_config)
    if problem:
        raise ValidationFailedError(problem)

    model = Model(
        organization_id=current_user.organization_id,
        name=payload.name,
        task=DbModelTask(payload.task.value),
        endpoint_url=payload.endpoint_url,
        identity_type=payload.identity_type.value,
        secret_ref=payload.secret_ref,
        identity_config=identity_config,
    )
    session.add(model)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="model.create",
        target_type="model",
        target_id=model.id,
        after=_audit_after(model),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(model)
    return _to_read(model)


@router.get(
    "/{model_id}",
    response_model=ModelRead,
    summary="Get a model by id",
)
async def get_model(
    model_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> ModelRead:
    """Return one model."""
    model = await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    return _to_read(model)


@router.patch(
    "/{model_id}",
    response_model=ModelRead,
    summary="Update a model (system administrators only)",
)
async def update_model(
    model_id: UUID,
    payload: ModelUpdate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> ModelRead:
    """Partially update a model; an omitted field is left untouched."""
    model = await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)

    updates = payload.model_dump(exclude_unset=True)
    if "task" in updates and updates["task"] is not None:
        updates["task"] = DbModelTask(updates["task"])
    if "identity_type" in updates and updates["identity_type"] is not None:
        updates["identity_type"] = ModelIdentity(updates["identity_type"]).value
    if "identity_config" in updates:
        # Replaced whole, not merged: switching identity drops the old ids.
        config = payload.identity_config
        updates["identity_config"] = config.model_dump(exclude_none=True) if config else {}

    problem = identity_problem(
        updates.get("identity_type") or model.identity_type,
        updates.get("secret_ref", model.secret_ref),
        updates.get("identity_config", model.identity_config),
    )
    if problem:
        raise ValidationFailedError(problem)

    for field, value in updates.items():
        setattr(model, field, value)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="model.update",
        target_type="model",
        target_id=model.id,
        after=_audit_after(model),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(model)
    return _to_read(model)


@router.delete(
    "/{model_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a model (system administrators only)",
)
async def delete_model(
    model_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
    settings: SettingsDep,
) -> None:
    """Delete a model: soft by default, hard with `APP_MODEL_DELETE_MODE=hard`.

    A soft-deleted model and its versions are gone from the API, but the rows
    stay, so every pre-label it wrote keeps its author. A hard delete removes
    them and is refused while any annotation was written by one of them: an
    annotation needs an author, and its history is never rewritten (DATA-1).
    """
    model = await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    mode = settings.model_delete_mode
    if mode == "hard":
        authored = await session.scalar(
            select(Annotation.id)
            .join(ModelVersion, ModelVersion.id == Annotation.author_model_version_id)
            .where(ModelVersion.model_id == model.id)
            .limit(1)
        )
        if authored is not None:
            raise ConflictError(
                f"Model '{model.name}' wrote annotations, so it can't be removed for good. "
                "Delete it with APP_MODEL_DELETE_MODE=soft (the default) instead."
            )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="model.delete",
        target_type="model",
        target_id=model.id,
        before={"name": model.name},
        after={"mode": mode},
        ip=client_ip,
    )
    if mode == "hard":
        await session.delete(model)
    else:
        model.deleted_at = datetime.now(UTC)
    await session.commit()


@router.post(
    "/{model_id}/check",
    response_model=ModelCheckResult,
    summary="Test a model endpoint's connectivity (system administrators only, BYOM-3)",
)
async def check_model(
    model_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
) -> ModelCheckResult:
    """`GET {endpoint_url}/info` with the model's credentials.

    A misconfigured or unreachable model must never fail the request: a bad
    secret reference, a network error, a rejection or a timeout all come
    back as `ok: false` with an explanatory message rather than propagating.
    """
    model = await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            client = await ModelClient.for_model(
                model.endpoint_url, model.identity_type, model.secret_ref, model.identity_config
            )
            try:
                info = await client.info()
            finally:
                await client.aclose()
    except (ModelUnavailable, ModelRejected, SecretResolutionError) as exc:
        return ModelCheckResult(ok=False, messages=[str(exc)])
    except TimeoutError:
        return ModelCheckResult(ok=False, messages=["The model endpoint timed out."])

    return ModelCheckResult(ok=True, messages=[], info=info)


@router.get(
    "/{model_id}/versions",
    response_model=Page[ModelVersionRead],
    summary="List a model's versions, oldest first",
)
async def list_model_versions(
    model_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page_params: PageParamsDep,
) -> Page[ModelVersionRead]:
    """List a model's versions, oldest first."""
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)

    stmt = select(ModelVersion).where(ModelVersion.model_id == model_id)
    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(ModelVersion.version, ModelVersion.id),
        key_of=lambda row: (row.version, row.id),
        direction="asc",
    )
    return Page[ModelVersionRead](
        items=[_version_to_read(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.get(
    "/{model_id}/family",
    response_model=ModelFamily,
    summary="Versions connected to this model's through parent links (EXP-8)",
)
async def get_model_family(
    model_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> ModelFamily:
    """The derivation graph around a model: teachers, students, quantized copies.

    Walks `parent_version_id` both ways from every version of the model, into
    any model of the organisation, and returns the connected versions oldest
    first. An organisation holds tens to hundreds of versions, so the walk is
    done in memory over one query.
    """
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    rows = (
        await session.execute(
            select(ModelVersion, Model.name, Model.task)
            .join(Model, Model.id == ModelVersion.model_id)
            .where(
                Model.organization_id == current_user.organization_id,
                Model.deleted_at.is_(None),
            )
        )
    ).all()
    neighbours: dict[UUID, set[UUID]] = {row[0].id: set() for row in rows}
    for version, _, _ in rows:
        parent = version.parent_version_id
        if parent is not None and parent in neighbours:
            neighbours[version.id].add(parent)
            neighbours[parent].add(version.id)
    connected = {version.id for version, _, _ in rows if version.model_id == model_id}
    frontier = list(connected)
    while frontier:
        for other in neighbours[frontier.pop()]:
            if other not in connected:
                connected.add(other)
                frontier.append(other)
    family = [
        ModelFamilyVersion(
            **_version_to_read(version).model_dump(),
            model_name=name,
            model_task=ModelTask(task.value),
        )
        for version, name, task in rows
        if version.id in connected
    ]
    family.sort(key=lambda v: (v.created_at, v.model_name, v.version))
    return ModelFamily(versions=family)


@router.post(
    "/{model_id}/versions",
    response_model=ModelVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a version to a model (system administrators only, BYOM-2)",
)
async def create_model_version(
    model_id: UUID,
    payload: ModelVersionCreate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> ModelVersionRead:
    """Add a new version with a `class_mapping` (BYOM-2).

    `version` defaults to one past the model's current highest version.
    """
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    return await add_version(session, current_user, client_ip, model_id, payload)


async def add_version(
    session: AsyncSession,
    current_user: CurrentUser,
    client_ip: str | None,
    model_id: UUID,
    payload: ModelVersionCreate,
) -> ModelVersionRead:
    """Insert a version of a model the caller may administer; commits.

    Shared by `POST /models/{id}/versions` and the MLflow import (API-6), so
    a version from a run passes the same lineage check as one added by hand.
    """
    try:
        class_mapping = normalise_mapping(payload.class_mapping)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    snapshot_id, snapshot_digest = await _resolve_lineage(
        session, current_user.organization_id, payload
    )
    if payload.parent_version_id is not None:
        parent = await session.scalar(
            select(ModelVersion.id)
            .join(Model, Model.id == ModelVersion.model_id)
            .where(
                ModelVersion.id == payload.parent_version_id,
                Model.organization_id == current_user.organization_id,
                Model.deleted_at.is_(None),
            )
        )
        if parent is None:
            raise NotFoundError(f"ModelVersion {payload.parent_version_id} does not exist.")

    if payload.version is None:
        current_max = await session.scalar(
            select(ModelVersion.version)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version.desc())
            .limit(1)
        )
        version_number = (current_max or 0) + 1
    else:
        version_number = payload.version
        existing = await session.scalar(
            select(ModelVersion.id).where(
                ModelVersion.model_id == model_id,
                ModelVersion.version == version_number,
            )
        )
        if existing is not None:
            raise ConflictError(f"Model {model_id} already has a version {version_number}.")

    version = ModelVersion(
        model_id=model_id,
        version=version_number,
        class_mapping=class_mapping,
        metrics=payload.metrics,
        snapshot_id=snapshot_id,
        snapshot_digest=snapshot_digest,
        training_run=payload.training_run,
        parent_version_id=payload.parent_version_id,
        derivation=payload.derivation.value if payload.derivation else None,
    )
    session.add(version)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="model_version.create",
        target_type="model_version",
        target_id=version.id,
        after={
            "version": version.version,
            "snapshot_id": str(snapshot_id) if snapshot_id else None,
            "snapshot_digest": snapshot_digest,
            "parent_version_id": str(payload.parent_version_id)
            if payload.parent_version_id
            else None,
            "derivation": version.derivation,
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(version)
    return _version_to_read(version)


@router.get(
    "/{model_id}/versions/{version_id}",
    response_model=ModelVersionRead,
    summary="Get a model version by id",
)
async def get_model_version(
    model_id: UUID,
    version_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> ModelVersionRead:
    """Return one model version, 404 if it does not belong to `model_id`."""
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)

    version = await session.get(ModelVersion, version_id)
    if version is None or version.model_id != model_id:
        raise NotFoundError(f"ModelVersion {version_id} does not exist.")
    return _version_to_read(version)


@router.get(
    "/{model_id}/versions/{version_id}/metrics",
    response_model=CorrectionMetrics,
    summary="How much humans corrected this version's pre-labels (ML-5)",
)
async def get_correction_metrics(
    model_id: UUID,
    version_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    project_id: Annotated[UUID | None, Query()] = None,
) -> CorrectionMetrics:
    """Compare the version's drafts with the final human version of each item.

    `project_id` narrows to one project (class mappings differ per project,
    so per-project numbers are the ones to read); it must belong to the
    caller's organisation.
    """
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    version = await session.get(ModelVersion, version_id)
    if version is None or version.model_id != model_id:
        raise NotFoundError(f"ModelVersion {version_id} does not exist.")
    if project_id is not None:
        await get_or_404(session, Project, project_id, organization_id=current_user.organization_id)
    return await correction_metrics(session, version_id=version.id, project_id=project_id)
