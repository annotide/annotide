"""Read access to the audit log (SEC-3).

Rows are written by the routers that make the changes (see
`app/services/audit.py`); this module only lists them. Organisation-scoped
and administrators only: the log names who did what, which is itself
sensitive. Without the `audit_history` feature only the last 30 days are
listed; every event is still recorded (LIC-33).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import LicenceDep, PageParamsDep, SessionDep, SuperuserDep
from app.models import AuditEvent
from app.schemas import AuditEventRead, Page
from app.services.licensing.features import Feature, has_feature
from app.services.repository import paginate

router = APIRouter(prefix="/audit", tags=["audit"])

#: How far back the log reads without `audit_history` (LIC-33).
UNLICENSED_HISTORY: Final = timedelta(days=30)


@router.get(
    "",
    response_model=Page[AuditEventRead],
    summary="List audit events for the caller's organisation (system administrators only)",
)
async def list_audit_events(
    session: SessionDep,
    current_user: SuperuserDep,
    page: PageParamsDep,
    licence: LicenceDep,
    action: Annotated[str | None, Query(description="Prefix match, e.g. `membership.`")] = None,
    target_type: Annotated[str | None, Query()] = None,
    target_id: Annotated[UUID | None, Query()] = None,
    actor_id: Annotated[UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="`created_at >=`")] = None,
) -> Page[AuditEventRead]:
    """Newest first, cursor-paginated, never crossing an organisation boundary."""
    stmt = select(AuditEvent).where(AuditEvent.organization_id == current_user.organization_id)
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action))
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    if not has_feature(licence, Feature.AUDIT_HISTORY):
        floor = datetime.now(UTC) - UNLICENSED_HISTORY
        since = floor if since is None or since < floor else since
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)

    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(AuditEvent.created_at, AuditEvent.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[AuditEventRead](
        items=[AuditEventRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


__all__ = ["router"]
