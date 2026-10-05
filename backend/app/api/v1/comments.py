"""Comment threads on items and annotations (WF-5).

Mentions and reply notifications are raised by `app.services.comments`; this
router only checks membership, validates that the targets belong to the
item, and writes the audit row in the same transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import ClientIpDep, CurrentUserDep, SessionDep
from app.api.errors import ForbiddenError, NotFoundError
from app.models import Annotation, Comment, Item
from app.schemas import CommentCreate, CommentRead, CommentResolve
from app.services import audit
from app.services.comments import create_comment
from app.services.repository import ensure_item_member, ensure_project_member, get_or_404

router = APIRouter(tags=["comments"])

#: Roles that may resolve someone else's comment (the author always may).
_CAN_RESOLVE = frozenset({"owner", "reviewer"})


@router.get(
    "/items/{item_id}/comments",
    response_model=list[CommentRead],
    summary="The comment thread on an item",
)
async def list_comments(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> list[Comment]:
    """Every comment on the item, oldest first — a plain array (bounded per item).

    Annotation-level comments carry the item id too, so they are included.
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)
    rows = await session.scalars(
        select(Comment)
        .where(Comment.item_id == item_id)
        .order_by(Comment.created_at.asc(), Comment.id.asc())
    )
    return list(rows)


@router.post(
    "/items/{item_id}/comments",
    response_model=CommentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment, notifying anyone @mentioned",
)
async def add_comment(
    item_id: UUID,
    payload: CommentCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Comment:
    """Append to the thread. `annotation_id` and `parent_id` must belong to this item."""
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)

    if payload.annotation_id is not None:
        annotation = await session.get(Annotation, payload.annotation_id)
        if annotation is None or annotation.item_id != item_id:
            raise NotFoundError(f"Annotation {payload.annotation_id} is not on this item.")
    if payload.parent_id is not None:
        parent = await session.get(Comment, payload.parent_id)
        if parent is None or parent.item_id != item_id:
            raise NotFoundError(f"Comment {payload.parent_id} is not on this item.")

    comment, notifications = await create_comment(
        session,
        project_id=item.project_id,
        item_id=item_id,
        author_id=current_user.id,
        body=payload.body,
        annotation_id=payload.annotation_id,
        parent_id=payload.parent_id,
        anchor=payload.anchor,
    )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="comment.create",
        target_type="comment",
        target_id=comment.id,
        after={
            "item_id": str(item_id),
            "annotation_id": str(payload.annotation_id) if payload.annotation_id else None,
            "parent_id": str(payload.parent_id) if payload.parent_id else None,
            "notifications": len(notifications),
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(comment)
    return comment


@router.post(
    "/comments/{comment_id}/resolve",
    response_model=CommentRead,
    summary="Resolve or reopen a comment",
)
async def resolve_comment(
    comment_id: UUID,
    payload: CommentResolve,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Comment:
    """The author, a reviewer or an owner may resolve; anyone else gets 403."""
    comment = await get_or_404(session, Comment, comment_id)
    if comment.item_id is None:
        role = await ensure_project_member(session, comment.project_id, current_user)
    else:
        item = await get_or_404(session, Item, comment.item_id)
        role = await ensure_item_member(session, item, current_user)
    if comment.author_id != current_user.id and role not in _CAN_RESOLVE:
        raise ForbiddenError("Only the author, a reviewer or an owner may resolve a comment.")

    was_resolved = comment.resolved_at is not None
    comment.resolved_at = datetime.now(UTC) if payload.resolved else None
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="comment.resolve",
        target_type="comment",
        target_id=comment.id,
        before={"resolved": was_resolved},
        after={"resolved": payload.resolved},
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(comment)
    return comment


__all__ = ["router"]
