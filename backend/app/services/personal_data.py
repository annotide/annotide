"""GDPR access and erasure for one person (SEC-6).

Access collects every row that is about the person into one export. Erasure
pseudonymises the `user` row instead of deleting it: annotations, audit rows,
snapshots and the annotation blobs in customer storage reference its `id`,
which carries no personal data once the row is scrubbed. The rules are in
docs/CONTRACTS.md → "### user".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    Annotation,
    ApiKey,
    AuditEvent,
    Comment,
    Membership,
    Notification,
    Project,
    Task,
    TaskStatus,
    User,
)
from app.schemas.personal_data import (
    PersonalAnnotation,
    PersonalApiKey,
    PersonalAuditEvent,
    PersonalComment,
    PersonalDataExport,
    PersonalMembership,
    PersonalNotification,
    PersonalProfile,
    PersonalTask,
)
from app.services import audit

ERASED_DISPLAY_NAME = "Erased user"
ERASED_COMMENT_BODY = "[erased]"
_LIVE_TASK_STATUSES = (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)


def erased_email(user_id: UUID) -> str:
    return f"erased-{user_id}@erased.invalid"


async def _get_user(session: AsyncSession, organization_id: UUID, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or user.organization_id != organization_id:
        raise NotFoundError(f"User {user_id} does not exist.")
    return user


def _audit_rows_about(user_id: UUID) -> Any:
    return or_(
        AuditEvent.actor_id == user_id,
        (AuditEvent.target_type == "user") & (AuditEvent.target_id == user_id),
    )


async def list_people(
    session: AsyncSession, *, organization_id: UUID, q: str | None = None, limit: int = 100
) -> list[User]:
    """The organisation's people (no service accounts), erased ones included, by e-mail."""
    stmt = select(User).where(User.organization_id == organization_id, User.is_service.is_(False))
    if q and q.strip():
        needle = q.strip()
        stmt = stmt.where(
            or_(
                User.email.icontains(needle, autoescape=True),
                User.display_name.icontains(needle, autoescape=True),
            )
        )
    result = await session.scalars(stmt.order_by(User.email, User.id).limit(limit))
    return list(result)


async def export_personal_data(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    actor_id: UUID,
    ip: str | None = None,
) -> PersonalDataExport:
    """Everything held about `user_id`, and an audit row saying who asked."""
    user = await _get_user(session, organization_id, user_id)
    profile = PersonalProfile(
        id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        is_service=user.is_service,
        idp_linked=user.idp_subject is not None,
        mfa_enabled=user.mfa_enabled,
        last_seen_at=user.last_seen_at,
        erased_at=user.erased_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )

    memberships = (
        await session.execute(
            select(Membership, Project.name)
            .join(Project, Project.id == Membership.project_id)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at)
        )
    ).all()
    api_keys = (
        await session.scalars(
            select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at)
        )
    ).all()
    comments = (
        await session.scalars(
            select(Comment).where(Comment.author_id == user_id).order_by(Comment.created_at)
        )
    ).all()
    annotations = (
        await session.scalars(
            select(Annotation)
            .where(Annotation.author_user_id == user_id)
            .order_by(Annotation.created_at)
        )
    ).all()
    tasks = (
        await session.scalars(
            select(Task)
            .where(or_(Task.assignee_id == user_id, Task.locked_by_id == user_id))
            .order_by(Task.id)
        )
    ).all()
    notifications = (
        await session.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at)
        )
    ).all()
    events = (
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id, _audit_rows_about(user_id))
            .order_by(AuditEvent.created_at)
        )
    ).all()

    export = PersonalDataExport(
        generated_at=datetime.now(UTC),
        user=profile,
        memberships=[
            PersonalMembership(
                project_id=m.project_id, project_name=name, role=m.role, created_at=m.created_at
            )
            for m, name in memberships
        ],
        api_keys=[
            PersonalApiKey(
                id=k.id,
                name=k.name,
                scopes=list(k.scopes),
                created_at=k.created_at,
                expires_at=k.expires_at,
                last_used_at=k.last_used_at,
                revoked_at=k.revoked_at,
            )
            for k in api_keys
        ],
        comments=[
            PersonalComment(
                id=c.id,
                project_id=c.project_id,
                item_id=c.item_id,
                annotation_id=c.annotation_id,
                body=c.body,
                created_at=c.created_at,
                resolved_at=c.resolved_at,
            )
            for c in comments
        ],
        annotations=[
            PersonalAnnotation(
                id=a.id,
                item_id=a.item_id,
                version=a.version,
                status=a.status,
                kind=a.kind,
                duration_ms=a.duration_ms,
                created_at=a.created_at,
            )
            for a in annotations
        ],
        tasks=[
            PersonalTask(
                id=t.id, project_id=t.project_id, item_id=t.item_id, type=t.type, status=t.status
            )
            for t in tasks
        ],
        notifications=[
            PersonalNotification(
                id=n.id,
                type=n.type,
                payload=dict(n.payload),
                read_at=n.read_at,
                created_at=n.created_at,
            )
            for n in notifications
        ],
        audit_events=[
            PersonalAuditEvent(
                id=e.id,
                action=e.action,
                target_type=e.target_type,
                target_id=e.target_id,
                ip=None if e.ip is None else str(e.ip),
                created_at=e.created_at,
            )
            for e in events
        ],
    )
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="user.export_personal_data",
        target_type="user",
        target_id=user_id,
        ip=ip,
    )
    return export


def _without_email(snapshot: dict[str, object] | None) -> dict[str, object] | None:
    if snapshot is None or "email" not in snapshot:
        return snapshot
    return {key: value for key, value in snapshot.items() if key != "email"}


async def erase_user(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    confirm_email: str,
    redact_comments: bool,
    actor_id: UUID,
    ip: str | None = None,
) -> User:
    """Pseudonymise `user_id` in the caller's transaction. The caller commits."""
    user = await _get_user(session, organization_id, user_id)
    if user_id == actor_id:
        raise ConflictError("You cannot erase your own account; ask another administrator.")
    if user.erased_at is not None:
        raise ConflictError(f"User {user_id} is already erased.")
    if confirm_email.strip().lower() != user.email.lower():
        raise ValidationFailedError("confirm_email does not match the user's e-mail.")

    now = datetime.now(UTC)
    user.email = erased_email(user.id)
    user.display_name = ERASED_DISPLAY_NAME
    user.idp_subject = None
    user.password_hash = None
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.mfa_recovery_codes = None
    user.last_seen_at = None
    user.is_active = False
    user.is_superuser = False
    user.erased_at = now

    await session.execute(delete(Membership).where(Membership.user_id == user_id))
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(Task)
        .where(
            or_(Task.assignee_id == user_id, Task.locked_by_id == user_id),
            Task.status.in_(_LIVE_TASK_STATUSES),
        )
        .values(status=TaskStatus.OPEN, assignee_id=None, locked_by_id=None, locked_until=None)
    )
    if redact_comments:
        await session.execute(
            update(Comment).where(Comment.author_id == user_id).values(body=ERASED_COMMENT_BODY)
        )

    # The one sanctioned edit of audit rows (SEC-3 vs SEC-6): drop what
    # identifies the person, keep what happened.
    events = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.organization_id == organization_id, _audit_rows_about(user_id)
            )
        )
    ).all()
    for event in events:
        event.ip = None
        event.before = _without_email(event.before)
        event.after = _without_email(event.after)

    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="user.erase",
        target_type="user",
        target_id=user_id,
        after={"redact_comments": redact_comments},
        ip=ip,
    )
    return user


__all__ = ["erase_user", "erased_email", "export_personal_data"]
