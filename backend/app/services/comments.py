"""Comment threads, @-mentions and the notifications they raise (WF-5).

A mention is a literal `@` immediately followed by the e-mail address of a
user in the same organisation as the comment's project (`@anna@example.com`).
An address that does not resolve to a member of that organisation is left as
plain text — no notification, no error. A reply (`parent_id` set) notifies
the parent comment's author unless they are the replier or were already
notified as a mention on the same comment.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Annotation,
    Comment,
    Item,
    Notification,
    NotificationType,
    Project,
    User,
)

#: `@` followed immediately by an e-mail address. The domain requires at
#: least one `.label` after the host so trailing sentence punctuation (a
#: period, a comma) is never swallowed into the match, and a bare `@` or
#: `@word` (no embedded `@`) never matches at all.
MENTION_RE = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

#: First N characters of a comment body carried in a notification payload.
_EXCERPT_LENGTH = 200


def extract_mentions(body: str) -> list[str]:
    """Every distinct e-mail address mentioned in `body`, lower-cased, in order.

    De-duplicates so a name mentioned twice only ever resolves to one
    notification per recipient.
    """
    seen: set[str] = set()
    mentions: list[str] = []
    for match in MENTION_RE.finditer(body):
        email = match.group(1).lower()
        if email not in seen:
            seen.add(email)
            mentions.append(email)
    return mentions


def _payload(
    *, project_id: UUID, item_id: UUID | None, actor_id: UUID, comment_id: UUID, excerpt: str
) -> dict[str, Any]:
    return {
        "project_id": str(project_id),
        "item_id": str(item_id) if item_id is not None else None,
        "actor_id": str(actor_id),
        "comment_id": str(comment_id),
        "excerpt": excerpt,
    }


async def create_comment(
    session: AsyncSession,
    *,
    project_id: UUID,
    item_id: UUID,
    author_id: UUID,
    body: str,
    annotation_id: UUID | None = None,
    parent_id: UUID | None = None,
    anchor: dict[str, Any] | None = None,
) -> tuple[Comment, list[Notification]]:
    """Add a comment and the mention/reply notifications it raises.

    Does not commit; the caller's transaction covers the comment, every
    notification and (typically) an audit row together.
    """
    comment = Comment(
        project_id=project_id,
        item_id=item_id,
        annotation_id=annotation_id,
        parent_id=parent_id,
        author_id=author_id,
        body=body,
        anchor=anchor,
    )
    session.add(comment)
    await session.flush()  # assigns comment.id

    excerpt = body[:_EXCERPT_LENGTH]
    notifications: list[Notification] = []
    notified_user_ids: set[UUID] = {author_id}

    mentioned_emails = extract_mentions(body)
    if mentioned_emails:
        project = await session.get(Project, project_id)
        if project is not None:
            mentioned_users = await session.scalars(
                select(User).where(
                    User.organization_id == project.organization_id,
                    User.email.in_(mentioned_emails),
                )
            )
            for user in mentioned_users:
                if user.id in notified_user_ids:
                    continue
                notification = Notification(
                    user_id=user.id,
                    type=NotificationType.MENTION,
                    payload=_payload(
                        project_id=project_id,
                        item_id=item_id,
                        actor_id=author_id,
                        comment_id=comment.id,
                        excerpt=excerpt,
                    ),
                )
                session.add(notification)
                notifications.append(notification)
                notified_user_ids.add(user.id)

    if parent_id is not None:
        parent = await session.get(Comment, parent_id)
        if parent is not None and parent.author_id not in notified_user_ids:
            notification = Notification(
                user_id=parent.author_id,
                type=NotificationType.REPLY,
                payload=_payload(
                    project_id=project_id,
                    item_id=item_id,
                    actor_id=author_id,
                    comment_id=comment.id,
                    excerpt=excerpt,
                ),
            )
            session.add(notification)
            notifications.append(notification)
            notified_user_ids.add(parent.author_id)

    return comment, notifications


async def notify_review(
    session: AsyncSession,
    *,
    annotation: Annotation,
    item: Item,
    reviewer_id: UUID,
    approve: bool,
    comment: str | None,
) -> Notification | None:
    """Notify a submitted annotation's human author of a review verdict.

    Returns `None`, adding nothing, when there is no one to notify: the
    annotation was authored by a model, or the reviewer reviewed their own
    work.
    """
    author_id = annotation.author_user_id
    if author_id is None or author_id == reviewer_id:
        return None

    notification = Notification(
        user_id=author_id,
        type=NotificationType.REVIEW,
        payload={
            "project_id": str(item.project_id),
            "item_id": str(item.id),
            "actor_id": str(reviewer_id),
            "annotation_id": str(annotation.id),
            "approve": approve,
            "comment": comment,
        },
    )
    session.add(notification)
    return notification


__all__ = ["MENTION_RE", "create_comment", "extract_mentions", "notify_review"]
