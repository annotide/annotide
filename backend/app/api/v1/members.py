"""Project membership endpoints (§4 permissions, SEC-3).

Adding, changing or removing a member is the "permission change" the audit
log records (`membership.create` / `.update` / `.delete`, see
`docs/CONTRACTS.md` → "### audit_event"). All three mutating endpoints are
owner-only; the last owner of a project can never be demoted or removed,
since that would leave the project with nobody able to manage it.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClientIpDep, CurrentUserDep, LicenceDep, SessionDep
from app.api.errors import (
    ConflictError,
    ForbiddenError,
    LicenceFeatureError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import Membership, MembershipSource, User
from app.models import ProjectRole as ModelProjectRole
from app.schemas import MemberCreate, MemberRead, MemberUpdate
from app.services import audit
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.features import refusal_message as feature_refusal
from app.services.licensing.state import EffectiveLicense
from app.services.repository import ensure_project_member, get_or_404

router = APIRouter(prefix="/projects/{project_id}/members", tags=["members"])


async def _get_membership_or_404(
    session: AsyncSession, project_id: UUID, user_id: UUID
) -> Membership:
    membership = await session.scalar(
        select(Membership).where(Membership.project_id == project_id, Membership.user_id == user_id)
    )
    if membership is None:
        raise NotFoundError(f"User {user_id} is not a member of project {project_id}.")
    return membership


async def _count_owners(session: AsyncSession, project_id: UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.project_id == project_id, Membership.role == ModelProjectRole.OWNER)
    )
    return int(count or 0)


@router.get(
    "",
    response_model=list[MemberRead],
    summary="List a project's members",
)
async def list_members(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> list[MemberRead]:
    """List every member of a project, oldest membership first.

    Open to any project member (any role), not just the owner.
    """
    await ensure_project_member(session, project_id, current_user)

    rows = await session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.project_id == project_id)
        .order_by(Membership.created_at)
    )
    return [
        MemberRead(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role.value,
            source=membership.source.value,
            path_prefixes=membership.path_prefixes,
            created_at=membership.created_at,
        )
        for membership, user in rows.all()
    ]


@router.post(
    "",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member to a project (owner only)",
)
async def add_member(
    project_id: UUID,
    payload: MemberCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    licence: LicenceDep,
) -> MemberRead:
    """Add a user to a project by `user_id` or `email`, with a role.

    The user must already exist in the caller's organization; a `user_id` or
    `email` from another organization is reported as `404`, never `403`, so a
    caller cannot use this endpoint to enumerate other tenants' accounts.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may add members.")
    if payload.path_prefixes:
        _require_path_permissions(licence)

    user: User
    if payload.user_id is not None:
        user = await get_or_404(
            session, User, payload.user_id, organization_id=current_user.organization_id
        )
    else:
        assert payload.email is not None  # guaranteed by MemberCreate's validator
        found = await session.scalar(
            select(User).where(
                User.email == payload.email,
                User.organization_id == current_user.organization_id,
            )
        )
        if found is None:
            raise NotFoundError(f"No user with email {payload.email!r} in this organization.")
        user = found

    existing = await session.scalar(
        select(Membership).where(Membership.project_id == project_id, Membership.user_id == user.id)
    )
    if existing is not None:
        raise ConflictError(f"User {user.id} is already a member of project {project_id}.")

    membership = Membership(
        user_id=user.id,
        project_id=project_id,
        role=ModelProjectRole(payload.role.value),
        path_prefixes=payload.path_prefixes,
    )
    session.add(membership)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="membership.create",
        target_type="membership",
        target_id=membership.id,
        after={
            "user_id": str(user.id),
            "role": payload.role.value,
            "path_prefixes": payload.path_prefixes,
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(membership)
    return MemberRead(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role.value,
        source=membership.source.value,
        path_prefixes=membership.path_prefixes,
        created_at=membership.created_at,
    )


@router.patch(
    "/{user_id}",
    response_model=MemberRead,
    summary="Change a member's role (owner only)",
)
async def update_member(
    project_id: UUID,
    user_id: UUID,
    payload: MemberUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
    licence: LicenceDep,
) -> MemberRead:
    """Change a member's role.

    Refuses (`409`) to demote the project's last remaining owner: someone
    must always be able to manage the project's membership.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may change a member's role.")

    membership = await _get_membership_or_404(session, project_id, user_id)
    changes = payload.model_fields_set
    new_role = (
        ModelProjectRole(payload.role.value)
        if "role" in changes and payload.role is not None
        else membership.role
    )
    new_prefixes = payload.path_prefixes if "path_prefixes" in changes else membership.path_prefixes
    if (new_prefixes or None) != (membership.path_prefixes or None):
        # Stored limits stay enforced without the licence; changing them needs it.
        _require_path_permissions(licence)

    if (
        membership.role == ModelProjectRole.OWNER
        and new_role != ModelProjectRole.OWNER
        and await _count_owners(session, project_id) <= 1
    ):
        raise ConflictError("The last owner of a project cannot be demoted.")
    if new_role == ModelProjectRole.OWNER and new_prefixes is not None:
        raise ValidationFailedError("An owner sees the whole project; path_prefixes must be null.")

    before = {
        "user_id": str(user_id),
        "role": membership.role.value,
        "path_prefixes": membership.path_prefixes,
    }
    membership.role = new_role
    membership.path_prefixes = new_prefixes
    membership.source = MembershipSource.MANUAL  # an owner's choice outranks group sync
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="membership.update",
        target_type="membership",
        target_id=membership.id,
        before=before,
        after={
            "user_id": str(user_id),
            "role": new_role.value,
            "path_prefixes": new_prefixes,
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(membership)

    user = await get_or_404(session, User, user_id)
    return MemberRead(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role.value,
        source=membership.source.value,
        path_prefixes=membership.path_prefixes,
        created_at=membership.created_at,
    )


@router.delete(
    "/{user_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from a project (owner only)",
)
async def remove_member(
    project_id: UUID,
    user_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Response:
    """Remove a member from a project.

    Refuses (`409`) to remove the project's last remaining owner.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may remove a member.")

    membership = await _get_membership_or_404(session, project_id, user_id)

    if membership.role == ModelProjectRole.OWNER and await _count_owners(session, project_id) <= 1:
        raise ConflictError("The last owner of a project cannot be removed.")

    before = {"user_id": str(user_id), "role": membership.role.value}
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="membership.delete",
        target_type="membership",
        target_id=membership.id,
        before=before,
        ip=client_ip,
    )
    await session.delete(membership)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]


def _require_path_permissions(licence: EffectiveLicense) -> None:
    """Folder-level permissions are a Business feature (LIC-33)."""
    if not has_feature(licence, Feature.PATH_PERMISSIONS):
        raise LicenceFeatureError(feature_refusal(Feature.PATH_PERMISSIONS))
