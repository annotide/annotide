"""IdP group sync at SSO sign-in (AUTH-3).

The groups claim of a verified ID token decides two things: project
memberships, through each project's `settings.idp_groups` map, and — when
`APP_OIDC_ADMIN_GROUPS` is set — the `is_superuser` flag. Sync only ever
touches memberships it created (`source = idp`); a membership an owner added
by hand is left alone. It never removes the last owner of a project nor the
last superuser of an organisation. Rules: docs/CONTRACTS.md → "### membership".
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, MembershipSource, Project, ProjectRole, User
from app.services import audit

log = structlog.get_logger(__name__)

_RANK = {
    ProjectRole.VIEWER: 0,
    ProjectRole.ANNOTATOR: 1,
    ProjectRole.REVIEWER: 2,
    ProjectRole.OWNER: 3,
}


def parse_admin_groups(value: str | None) -> frozenset[str] | None:
    """`APP_OIDC_ADMIN_GROUPS` as a set; `None` when unset (superuser not managed)."""
    if value is None or not value.strip():
        return None
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def role_for(groups: Iterable[str], mapping: object) -> ProjectRole | None:
    """The highest role `mapping` (`settings.idp_groups`) grants any of `groups`."""
    if not isinstance(mapping, dict):
        return None
    roles: list[ProjectRole] = []
    for group in groups:
        role = mapping.get(group)
        if role in _RANK:
            roles.append(ProjectRole(role))
    return max(roles, key=_RANK.__getitem__, default=None)


async def _other_owners(session: AsyncSession, membership: Membership) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.project_id == membership.project_id,
            Membership.role == ProjectRole.OWNER,
            Membership.id != membership.id,
        )
    )
    return int(count or 0)


async def _other_superusers(session: AsyncSession, user: User) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.organization_id == user.organization_id,
            User.is_superuser.is_(True),
            User.is_active.is_(True),
            User.id != user.id,
        )
    )
    return int(count or 0)


async def sync_groups(
    session: AsyncSession,
    user: User,
    groups: Iterable[str],
    *,
    admin_groups: frozenset[str] | None,
    ip: str | None = None,
    scim: bool = False,
) -> None:
    """Bring `user`'s superuser flag and `idp` memberships in line with `groups`.

    `scim` marks a change pushed by the IdP over SCIM rather than made at the
    user's own sign-in: it is audited with no actor and `via: scim`.
    Runs in the caller's transaction; the caller commits.
    """
    group_set = frozenset(groups)
    actor_id = None if scim else user.id
    await _sync_superuser(session, user, group_set, admin_groups, ip, actor_id)

    projects = (
        await session.scalars(
            select(Project).where(Project.organization_id == user.organization_id)
        )
    ).all()
    wanted: dict[UUID, ProjectRole] = {}
    for project in projects:
        role = role_for(group_set, (project.settings or {}).get("idp_groups"))
        if role is not None:
            wanted[project.id] = role
    project_ids = {project.id for project in projects}
    existing = {
        m.project_id: m
        for m in (
            await session.scalars(select(Membership).where(Membership.user_id == user.id))
        ).all()
        if m.project_id in project_ids
    }

    for project_id in sorted(wanted.keys() | existing.keys()):
        role = wanted.get(project_id)
        membership = existing.get(project_id)
        if membership is None:
            assert role is not None  # only wanted projects lack a membership
            membership = Membership(
                user_id=user.id, project_id=project_id, role=role, source=MembershipSource.IDP
            )
            session.add(membership)
            await session.flush()
            _audit(session, user, membership, "membership.create", None, role, ip, actor_id)
        elif membership.source != MembershipSource.IDP or membership.role == role:
            continue
        elif (
            membership.role == ProjectRole.OWNER
            and role != ProjectRole.OWNER
            and await _other_owners(session, membership) == 0
        ):
            log.warning("group_sync.kept_last_owner", project_id=str(project_id))
        elif role is None:
            _audit(
                session, user, membership, "membership.delete", membership.role, None, ip, actor_id
            )
            await session.delete(membership)
        else:
            before = membership.role
            membership.role = role
            _audit(session, user, membership, "membership.update", before, role, ip, actor_id)


async def _sync_superuser(
    session: AsyncSession,
    user: User,
    groups: frozenset[str],
    admin_groups: frozenset[str] | None,
    ip: str | None,
    actor_id: UUID | None,
) -> None:
    if admin_groups is None:
        return
    wanted = bool(groups & admin_groups)
    if wanted == user.is_superuser:
        return
    if not wanted and await _other_superusers(session, user) == 0:
        log.warning("group_sync.kept_last_superuser", user_id=str(user.id))
        return
    user.is_superuser = wanted
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=actor_id,
        action="user.superuser_sync",
        target_type="user",
        target_id=user.id,
        after=_via({"is_superuser": wanted}, actor_id),
        ip=ip,
    )


def _audit(
    session: AsyncSession,
    user: User,
    membership: Membership,
    action: str,
    before: ProjectRole | None,
    after: ProjectRole | None,
    ip: str | None,
    actor_id: UUID | None,
) -> None:
    def snapshot(role: ProjectRole | None) -> dict[str, object] | None:
        if role is None:
            return None
        return _via({"user_id": str(user.id), "role": role.value, "source": "idp"}, actor_id)

    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=actor_id,
        action=action,
        target_type="membership",
        target_id=membership.id,
        before=snapshot(before),
        after=snapshot(after),
        ip=ip,
    )


def _via(snapshot: dict[str, object], actor_id: UUID | None) -> dict[str, object]:
    """Mark a SCIM-driven change, which has no actor, in its audit snapshot."""
    return snapshot if actor_id is not None else {**snapshot, "via": "scim"}


__all__ = ["parse_admin_groups", "role_for", "sync_groups"]
