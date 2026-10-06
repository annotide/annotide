"""The caller's in-app notifications (WF-5).

Strictly personal: every query is filtered by `user_id = current_user.id`,
so another user's notification is a 404, never a 403 that would confirm it
exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import func, select, update

from app.api.deps import CurrentUserDep, PageParamsDep, SessionDep
from app.api.errors import NotFoundError
from app.models import Notification
from app.schemas import NotificationRead, Page, UnreadCount
from app.services.repository import paginate

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationRead], summary="The caller's notifications")
async def list_notifications(
    session: SessionDep,
    current_user: CurrentUserDep,
    page: PageParamsDep,
    unread: Annotated[bool, Query(description="Only unread ones")] = False,
) -> Page[NotificationRead]:
    """Newest first, cursor-paginated."""
    stmt = select(Notification).where(Notification.user_id == current_user.id)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(Notification.created_at, Notification.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[NotificationRead](
        items=[NotificationRead.model_validate(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


@router.get("/unread-count", response_model=UnreadCount, summary="How many are unread")
async def unread_count(session: SessionDep, current_user: CurrentUserDep) -> UnreadCount:
    count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
    )
    return UnreadCount(count=int(count or 0))


@router.post(
    "/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark every notification read",
)
async def read_all(session: SessionDep, current_user: CurrentUserDep) -> Response:
    await session.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark one notification read",
)
async def read_one(
    notification_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> Notification:
    """Idempotent: an already-read notification keeps its original `read_at`."""
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != current_user.id:
        raise NotFoundError(f"Notification {notification_id} does not exist.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(notification)
    return notification


__all__ = ["router"]
