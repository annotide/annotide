"""Webhook subscriptions and their delivery log (API-4); retraining requests (ML-9).

Who may manage a hook follows its scope: an organisation-wide hook
(`project_id` null) is superuser territory, a project hook belongs to that
project's owners. Reading a hook or its deliveries needs the same right —
a delivery payload can carry item paths and reviewer ids.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClientIpDep, CurrentUser, CurrentUserDep, PageParamsDep, SessionDep
from app.api.errors import (
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.models import (
    MlPlatform,
    MlPlatformKind,
    Project,
    Snapshot,
    Webhook,
    WebhookDelivery,
    WebhookFormat,
)
from app.models import WebhookDeliveryStatus as ModelDeliveryStatus
from app.schemas import (
    MlRun,
    Page,
    RetrainRequest,
    RetrainResult,
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryRead,
    WebhookRead,
    WebhookUpdate,
    WebhookUpdated,
)
from app.schemas import WebhookDeliveryStatus as SchemaDeliveryStatus
from app.services import audit, ml_platforms
from app.services.ml_platforms import MlPlatformNotFound, MlPlatformUnavailable
from app.services.repository import ensure_project_member, get_or_404, paginate
from app.services.secrets import SecretResolutionError
from app.services.webhooks import emit_event, generate_secret, seal_secret

router = APIRouter(tags=["webhooks"])


async def _require_scope_admin(
    session: AsyncSession, user: CurrentUser, project_id: UUID | None
) -> None:
    """Superuser for organisation-wide hooks; project owner for project hooks."""
    if project_id is None:
        if not user.is_superuser:
            raise ForbiddenError("Only a superuser may manage organisation-wide webhooks.")
        return
    await get_or_404(session, Project, project_id, organization_id=user.organization_id)
    role = await ensure_project_member(session, project_id, user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may manage its webhooks.")


async def _hook_or_404(session: AsyncSession, user: CurrentUser, webhook_id: UUID) -> Webhook:
    hook = await session.get(Webhook, webhook_id)
    if hook is None or hook.organization_id != user.organization_id:
        raise NotFoundError(f"Webhook {webhook_id} does not exist.")
    return hook


@router.get("/webhooks", response_model=Page[WebhookRead], summary="List webhooks")
async def list_webhooks(
    session: SessionDep,
    current_user: CurrentUserDep,
    page: PageParamsDep,
    project_id: Annotated[UUID | None, Query()] = None,
) -> Page[WebhookRead]:
    """Organisation-wide hooks for superusers; with `project_id`, that project's
    hooks for its owners (superusers see both kinds for the project)."""
    await _require_scope_admin(session, current_user, project_id)
    stmt = select(Webhook).where(Webhook.organization_id == current_user.organization_id)
    if project_id is None:
        stmt = stmt.where(Webhook.project_id.is_(None))
    elif current_user.is_superuser:
        stmt = stmt.where((Webhook.project_id == project_id) | Webhook.project_id.is_(None))
    else:
        stmt = stmt.where(Webhook.project_id == project_id)
    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(Webhook.created_at, Webhook.id),
        key_of=lambda row: (row.created_at, row.id),
    )
    return Page[WebhookRead](
        items=[WebhookRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "/webhooks",
    response_model=WebhookCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe a URL to events",
)
async def create_webhook(
    payload: WebhookCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> WebhookCreated:
    """Returns the hook with its signing `secret` — the only time it is shown."""
    await _require_scope_admin(session, current_user, payload.project_id)
    secret = generate_secret()
    hook = Webhook(
        organization_id=current_user.organization_id,
        project_id=payload.project_id,
        url=str(payload.url),
        description=payload.description,
        events=payload.events,
        secret=seal_secret(secret),
        is_active=payload.is_active,
        format=WebhookFormat(payload.format.value),
        created_by_id=current_user.id,
    )
    session.add(hook)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="webhook.create",
        target_type="webhook",
        target_id=hook.id,
        after={
            "url": hook.url,
            "events": hook.events,
            "format": hook.format.value,
            "project_id": str(payload.project_id),
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(hook)
    # The column holds the sealed value; the creator gets the clear secret once.
    return WebhookCreated(**WebhookRead.model_validate(hook).model_dump(), secret=secret)


@router.get("/webhooks/{webhook_id}", response_model=WebhookRead, summary="Get a webhook")
async def get_webhook(
    webhook_id: UUID, session: SessionDep, current_user: CurrentUserDep
) -> WebhookRead:
    hook = await _hook_or_404(session, current_user, webhook_id)
    await _require_scope_admin(session, current_user, hook.project_id)
    return WebhookRead.model_validate(hook)


@router.patch(
    "/webhooks/{webhook_id}",
    response_model=WebhookUpdated,
    summary="Update a webhook, optionally rotating its secret",
)
async def update_webhook(
    webhook_id: UUID,
    payload: WebhookUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> WebhookUpdated:
    """Only keys present in the body change. With `rotate_secret` the response
    carries the new `secret` once; otherwise `secret` is absent."""
    hook = await _hook_or_404(session, current_user, webhook_id)
    await _require_scope_admin(session, current_user, hook.project_id)

    changes = payload.model_dump(exclude_unset=True, exclude={"rotate_secret"})
    if "url" in changes:
        hook.url = str(payload.url)
    if "events" in changes and payload.events is not None:
        hook.events = payload.events
    if "description" in changes:
        hook.description = payload.description
    if "is_active" in changes and payload.is_active is not None:
        hook.is_active = payload.is_active
    if "format" in changes and payload.format is not None:
        hook.format = WebhookFormat(payload.format.value)
    secret: str | None = None
    if payload.rotate_secret:
        secret = generate_secret()
        hook.secret = seal_secret(secret)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="webhook.update",
        target_type="webhook",
        target_id=hook.id,
        after={**{k: str(v) for k, v in changes.items()}, "rotated": payload.rotate_secret},
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(hook)
    # Built from the read view, not the row: `model_validate(hook)` would
    # pick up the stored secret through `from_attributes`.
    return WebhookUpdated(**WebhookRead.model_validate(hook).model_dump(), secret=secret)


@router.delete(
    "/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a webhook"
)
async def delete_webhook(
    webhook_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Response:
    hook = await _hook_or_404(session, current_user, webhook_id)
    await _require_scope_admin(session, current_user, hook.project_id)
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="webhook.delete",
        target_type="webhook",
        target_id=hook.id,
        before={"url": hook.url},
        ip=client_ip,
    )
    await session.delete(hook)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/webhooks/{webhook_id}/test",
    response_model=WebhookDeliveryRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a webhook.test delivery",
)
async def test_webhook(
    webhook_id: UUID, session: SessionDep, current_user: CurrentUserDep
) -> WebhookDeliveryRead:
    """Queues one `webhook.test` event for this hook alone (whatever its
    subscriptions), so a subscriber can check signature verification."""
    hook = await _hook_or_404(session, current_user, webhook_id)
    await _require_scope_admin(session, current_user, hook.project_id)
    delivery = WebhookDelivery(
        webhook_id=hook.id,
        event="webhook.test",
        payload={
            "event": "webhook.test",
            "organization_id": str(hook.organization_id),
            "project_id": str(hook.project_id) if hook.project_id else None,
            "data": {"webhook_id": str(hook.id), "requested_by": str(current_user.id)},
        },
    )
    session.add(delivery)
    await session.commit()
    await session.refresh(delivery)
    return WebhookDeliveryRead.model_validate(delivery)


@router.get(
    "/webhooks/{webhook_id}/deliveries",
    response_model=Page[WebhookDeliveryRead],
    summary="Delivery log of a webhook, newest first",
)
async def list_deliveries(
    webhook_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page: PageParamsDep,
    delivery_status: Annotated[SchemaDeliveryStatus | None, Query(alias="status")] = None,
) -> Page[WebhookDeliveryRead]:
    hook = await _hook_or_404(session, current_user, webhook_id)
    await _require_scope_admin(session, current_user, hook.project_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook.id)
    if delivery_status is not None:
        stmt = stmt.where(WebhookDelivery.status == ModelDeliveryStatus(delivery_status.value))
    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(WebhookDelivery.created_at, WebhookDelivery.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[WebhookDeliveryRead](
        items=[WebhookDeliveryRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "/projects/{project_id}/retrain",
    response_model=RetrainResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask subscribers to retrain (ML-9)",
)
async def request_retrain(
    project_id: UUID,
    payload: RetrainRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> RetrainResult:
    """Emit `retrain.requested` to the project's webhook subscribers. The
    platform trains nothing itself: the customer's pipeline listens for this
    event, reads the snapshot it names and runs. Owner only. With
    `snapshot_id` the payload carries the snapshot's digest and blob path so
    the run can prove what it trained on (EXP-8)."""
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may request retraining.")
    project = await get_or_404(session, Project, project_id)

    data: dict[str, Any] = {
        "project_id": str(project_id),
        "requested_by": str(current_user.id),
        "model_id": str(payload.model_id) if payload.model_id else None,
        "note": payload.note,
        "snapshot": None,
    }
    if payload.snapshot_id is not None:
        snapshot = await session.get(Snapshot, payload.snapshot_id)
        if snapshot is None or snapshot.project_id != project_id:
            raise NotFoundError(f"Snapshot {payload.snapshot_id} does not exist.")
        data["snapshot"] = {
            "id": str(snapshot.id),
            "name": snapshot.name,
            "digest": snapshot.digest,
            "blob_path": snapshot.blob_path,
            "item_count": snapshot.item_count,
            "label_schema_version_id": str(snapshot.label_schema_version_id),
            "split": snapshot.split,
        }

    ml_run: MlRun | None = None
    if payload.ml_platform_id is not None:
        ml_run = await _start_retrain_job(session, current_user, payload.ml_platform_id, data)
    data["ml_run"] = ml_run.model_dump(mode="json") if ml_run is not None else None

    queued = await emit_event(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        event="retrain.requested",
        payload=data,
    )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="project.retrain",
        target_type="project",
        target_id=project.id,
        after={
            "snapshot_id": str(payload.snapshot_id),
            "deliveries": queued,
            "ml_run_id": ml_run.run_id if ml_run is not None else None,
        },
        ip=client_ip,
    )
    await session.commit()
    return RetrainResult(event="retrain.requested", deliveries=queued, ml_run=ml_run)


async def _start_retrain_job(
    session: AsyncSession, user: CurrentUser, platform_id: UUID, data: dict[str, Any]
) -> MlRun:
    """Start the platform's Databricks job with the request as its parameters (API-6).

    Runs before the event is emitted: a job that did not start is a 503 and
    nobody is told a retrain began.
    """
    platform = await get_or_404(
        session, MlPlatform, platform_id, organization_id=user.organization_id
    )
    if platform.kind is not MlPlatformKind.DATABRICKS or not platform.config.get("job_id"):
        raise ValidationFailedError(
            "Only a databricks ML platform with config.job_id can run a retrain job; "
            "other platforms take the retrain.requested webhook."
        )
    snapshot = data["snapshot"] or {}
    parameters = {
        "project_id": data["project_id"],
        "snapshot_id": snapshot.get("id") or "",
        "snapshot_digest": snapshot.get("digest") or "",
        "snapshot_blob_path": snapshot.get("blob_path") or "",
        "model_id": data["model_id"] or "",
        "note": data["note"] or "",
    }
    try:
        run_id, run_url = await ml_platforms.run_retrain_job(platform, parameters)
    except (MlPlatformUnavailable, MlPlatformNotFound, SecretResolutionError) as exc:
        raise ServiceUnavailableError(f"The Databricks job did not start: {exc}") from None
    return MlRun(ml_platform_id=platform.id, run_id=run_id, run_url=run_url)
