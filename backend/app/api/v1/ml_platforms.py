"""ML platforms — MLflow, Databricks, Azure ML (API-6).

Registering and testing a platform is system administration, like models
and connectors; listing is open to the organisation so a project owner can
pick one to publish a snapshot to. Also here: publishing a snapshot as an
MLflow run, and adding a model version from one. A failing platform is a
503 naming what it said, never a 500.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientIpDep,
    CurrentUserDep,
    PageParamsDep,
    SessionDep,
    SuperuserDep,
    require_feature,
)
from app.api.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.api.v1.models import add_version
from app.models import MlPlatform, Model, Project, Snapshot
from app.models import MlPlatformKind as DbKind
from app.schemas import (
    MlIdentity,
    MlPlatformCheckResult,
    MlPlatformCreate,
    MlPlatformKind,
    MlPlatformRead,
    MlPlatformUpdate,
    ModelVersionCreate,
    ModelVersionImport,
    ModelVersionRead,
    Page,
    SnapshotPublishRequest,
    SnapshotPublishResult,
)
from app.services import audit, ml_platforms
from app.services.licensing.features import Feature
from app.services.ml_platforms import MlPlatformNotFound, MlPlatformUnavailable
from app.services.repository import ensure_project_member, get_or_404, paginate
from app.services.secrets import SecretResolutionError

router = APIRouter(tags=["ml-platforms"])


def _to_read(platform: MlPlatform) -> MlPlatformRead:
    return MlPlatformRead(
        id=platform.id,
        organization_id=platform.organization_id,
        name=platform.name,
        kind=MlPlatformKind(platform.kind.value),
        tracking_uri=platform.tracking_uri,
        identity_type=MlIdentity(platform.identity_type),
        has_secret=platform.secret_ref is not None,
        config=dict(platform.config),
        created_at=platform.created_at,
        updated_at=platform.updated_at,
    )


def _audit_after(platform: MlPlatform) -> dict[str, object]:
    """Never `secret_ref`, only whether one is set."""
    return {
        "name": platform.name,
        "kind": platform.kind.value,
        "tracking_uri": platform.tracking_uri,
        "identity_type": platform.identity_type,
        "has_secret": platform.secret_ref is not None,
        "config": dict(platform.config),
    }


async def _name_taken(
    session: AsyncSession, organization_id: UUID, name: str, own_id: UUID | None = None
) -> bool:
    stmt = select(MlPlatform.id).where(
        MlPlatform.organization_id == organization_id, MlPlatform.name == name
    )
    if own_id is not None:
        stmt = stmt.where(MlPlatform.id != own_id)
    return await session.scalar(stmt) is not None


def _platform_failed(exc: Exception) -> ServiceUnavailableError:
    if isinstance(exc, SecretResolutionError):
        return ServiceUnavailableError(f"The ML platform's credential: {exc}")
    return ServiceUnavailableError(f"The ML platform failed: {exc}")


@router.get(
    "/ml-platforms",
    response_model=Page[MlPlatformRead],
    summary="List the organisation's ML platforms (API-6)",
)
async def list_platforms(
    session: SessionDep, current_user: CurrentUserDep, page_params: PageParamsDep
) -> Page[MlPlatformRead]:
    stmt = select(MlPlatform).where(MlPlatform.organization_id == current_user.organization_id)
    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(MlPlatform.name, MlPlatform.id),
        key_of=lambda row: (row.name, row.id),
        direction="asc",
    )
    return Page[MlPlatformRead](
        items=[_to_read(row) for row in result.items], next_cursor=result.next_cursor
    )


@router.post(
    "/ml-platforms",
    dependencies=[require_feature(Feature.ML_PLATFORMS)],
    response_model=MlPlatformRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register an MLflow, Databricks or Azure ML platform (system administrators, API-6)",
)
async def create_platform(
    payload: MlPlatformCreate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> MlPlatformRead:
    if await _name_taken(session, current_user.organization_id, payload.name):
        raise ConflictError(f"An ML platform named {payload.name!r} already exists.")
    platform = MlPlatform(
        organization_id=current_user.organization_id,
        name=payload.name,
        kind=DbKind(payload.kind.value),
        tracking_uri=payload.tracking_uri,
        identity_type=payload.identity_type.value,
        secret_ref=payload.secret_ref,
        config=payload.config,
    )
    session.add(platform)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="ml_platform.create",
        target_type="ml_platform",
        target_id=platform.id,
        after=_audit_after(platform),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(platform)
    return _to_read(platform)


@router.get("/ml-platforms/{platform_id}", response_model=MlPlatformRead, summary="Get one")
async def get_platform(
    platform_id: UUID, session: SessionDep, current_user: CurrentUserDep
) -> MlPlatformRead:
    platform = await get_or_404(
        session, MlPlatform, platform_id, organization_id=current_user.organization_id
    )
    return _to_read(platform)


@router.patch(
    "/ml-platforms/{platform_id}",
    dependencies=[require_feature(Feature.ML_PLATFORMS)],
    response_model=MlPlatformRead,
    summary="Update a platform (system administrators)",
)
async def update_platform(
    platform_id: UUID,
    payload: MlPlatformUpdate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> MlPlatformRead:
    """Partial update. The kind cannot change; the merged row must be valid as a whole."""
    platform = await get_or_404(
        session, MlPlatform, platform_id, organization_id=current_user.organization_id
    )
    updates = payload.model_dump(exclude_unset=True)
    merged: dict[str, Any] = {
        "name": platform.name,
        "kind": platform.kind.value,
        "tracking_uri": platform.tracking_uri,
        "identity_type": platform.identity_type,
        "secret_ref": platform.secret_ref,
        "config": dict(platform.config),
    }
    merged.update(updates)
    if (
        "identity_type" in updates
        and "secret_ref" not in updates
        and updates["identity_type"] in {"none", "managed_identity"}
    ):
        # Switching to an identity without a secret drops the old reference.
        merged["secret_ref"] = None
    try:
        valid = MlPlatformCreate.model_validate(merged)
    except ValidationError as exc:
        raise ValidationFailedError(
            "; ".join(str(error["msg"]) for error in exc.errors(include_url=False))
        ) from None
    if valid.name != platform.name and await _name_taken(
        session, current_user.organization_id, valid.name, platform.id
    ):
        raise ConflictError(f"An ML platform named {valid.name!r} already exists.")
    platform.name = valid.name
    platform.tracking_uri = valid.tracking_uri
    platform.identity_type = valid.identity_type.value
    platform.secret_ref = valid.secret_ref
    platform.config = valid.config
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="ml_platform.update",
        target_type="ml_platform",
        target_id=platform.id,
        after=_audit_after(platform),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(platform)
    return _to_read(platform)


@router.delete(
    "/ml-platforms/{platform_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a platform (system administrators)",
)
async def delete_platform(
    platform_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> None:
    """Runs already published stay on the platform and in versions' `training_run`."""
    platform = await get_or_404(
        session, MlPlatform, platform_id, organization_id=current_user.organization_id
    )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="ml_platform.delete",
        target_type="ml_platform",
        target_id=platform.id,
        before={"name": platform.name, "kind": platform.kind.value},
        ip=client_ip,
    )
    await session.delete(platform)
    await session.commit()


@router.post(
    "/ml-platforms/{platform_id}/check",
    dependencies=[require_feature(Feature.ML_PLATFORMS)],
    response_model=MlPlatformCheckResult,
    summary="Test a platform's address and credentials (system administrators)",
)
async def check_platform(
    platform_id: UUID, session: SessionDep, current_user: SuperuserDep
) -> MlPlatformCheckResult:
    """One experiment search. Every failure is `ok: false` with the reason."""
    platform = await get_or_404(
        session, MlPlatform, platform_id, organization_id=current_user.organization_id
    )
    try:
        info = await ml_platforms.check(platform)
    except (MlPlatformUnavailable, MlPlatformNotFound, SecretResolutionError) as exc:
        return MlPlatformCheckResult(ok=False, messages=[str(exc)])
    return MlPlatformCheckResult(ok=True, messages=[], info=info)


@router.post(
    "/projects/{project_id}/snapshots/{snapshot_id}/mlflow",
    dependencies=[require_feature(Feature.ML_PLATFORMS)],
    response_model=SnapshotPublishResult,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a snapshot to an ML platform as an MLflow run (API-6)",
)
async def publish_snapshot(
    project_id: UUID,
    snapshot_id: UUID,
    payload: SnapshotPublishRequest,
    response: Response,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> SnapshotPublishResult:
    """A FINISHED run with the snapshot as its dataset input; nothing is copied.

    Owner only. 200 with `created: false` when the snapshot already has a run
    in that experiment.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may publish snapshots.")
    project = await get_or_404(session, Project, project_id)
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.project_id != project_id:
        raise NotFoundError(f"Snapshot {snapshot_id} does not exist.")
    platform = await get_or_404(
        session, MlPlatform, payload.ml_platform_id, organization_id=current_user.organization_id
    )
    experiment = payload.experiment or ml_platforms.default_experiment(platform, project.name)
    facts = ml_platforms.SnapshotFacts(
        snapshot_id=str(snapshot.id),
        project_id=str(project.id),
        name=snapshot.name,
        digest=snapshot.digest,
        blob_path=snapshot.blob_path,
        item_count=snapshot.item_count,
        label_schema_version_id=str(snapshot.label_schema_version_id),
        result_connector_id=str(project.result_connector_id)
        if project.result_connector_id
        else None,
        split=snapshot.split,
    )
    try:
        published = await ml_platforms.publish_snapshot(platform, facts, experiment)
    except (MlPlatformUnavailable, MlPlatformNotFound, SecretResolutionError) as exc:
        raise _platform_failed(exc) from None
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="snapshot.publish",
        target_type="snapshot",
        target_id=snapshot.id,
        after={
            "ml_platform_id": str(platform.id),
            "experiment": experiment,
            "run_id": published.run_id,
            "created": published.created,
        },
        ip=client_ip,
    )
    await session.commit()
    if not published.created:
        response.status_code = status.HTTP_200_OK
    return SnapshotPublishResult(
        ml_platform_id=platform.id,
        experiment_id=published.experiment_id,
        experiment_name=published.experiment_name,
        run_id=published.run_id,
        run_url=published.run_url,
        created=published.created,
    )


@router.post(
    "/models/{model_id}/versions/import",
    dependencies=[require_feature(Feature.ML_PLATFORMS)],
    response_model=ModelVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a model version from an MLflow run (system administrators, API-6)",
)
async def import_model_version(
    model_id: UUID,
    payload: ModelVersionImport,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> ModelVersionRead:
    """The run's metrics, its `training_run` record and the snapshot it claims.

    Lineage is checked as for a version added by hand (EXP-8): a run whose
    `annotation.snapshot_digest` disagrees with the snapshot it names is a 409.
    """
    await get_or_404(session, Model, model_id, organization_id=current_user.organization_id)
    platform = await get_or_404(
        session, MlPlatform, payload.ml_platform_id, organization_id=current_user.organization_id
    )
    try:
        imported = await ml_platforms.read_run(
            platform,
            run_id=payload.run_id,
            registered_model=payload.registered_model,
            model_version=payload.model_version,
        )
    except MlPlatformNotFound as exc:
        raise NotFoundError(f"The ML platform has no such run or model version: {exc}") from None
    except (MlPlatformUnavailable, SecretResolutionError) as exc:
        raise _platform_failed(exc) from None

    try:
        snapshot_uuid = UUID(imported.snapshot_id) if imported.snapshot_id else None
        version_payload = ModelVersionCreate(
            version=payload.version,
            class_mapping=payload.class_mapping,
            metrics=imported.metrics,
            snapshot_id=snapshot_uuid,
            snapshot_digest=imported.snapshot_digest,
            training_run=imported.training_run,
        )
    except (ValueError, ValidationError):
        raise ValidationFailedError(
            "The run's annotation.snapshot_id / annotation.snapshot_digest tags are malformed."
        ) from None
    try:
        return await add_version(session, current_user, client_ip, model_id, version_payload)
    except NotFoundError:
        raise NotFoundError(
            f"The run names snapshot {imported.snapshot_id}, which is not in this organisation."
        ) from None
