"""User directory and GDPR access and erasure (SEC-6).

All routes need a person behind the token: personal data is neither read
nor erased with an API key. A user may export their own record; a superuser
may export or erase anyone in the organisation.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import ClientIpDep, PersonDep, SessionDep, require_superuser
from app.api.errors import ForbiddenError
from app.schemas import UserRead
from app.schemas.personal_data import EraseRequest, PersonalDataExport
from app.services import personal_data

router = APIRouter(tags=["users"])


@router.get(
    "/users",
    response_model=list[UserRead],
    dependencies=[Depends(require_superuser)],
    summary="List the organisation's people (superuser)",
)
async def list_users(
    session: SessionDep,
    current_user: PersonDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[UserRead]:
    rows = await personal_data.list_people(
        session, organization_id=current_user.organization_id, q=q, limit=limit
    )
    return [UserRead.model_validate(row) for row in rows]


@router.get(
    "/users/{user_id}/personal-data",
    response_model=PersonalDataExport,
    summary="Export everything held about a person (GDPR access)",
)
async def export_personal_data(
    user_id: UUID,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> PersonalDataExport:
    if user_id != current_user.id and not current_user.is_superuser:
        raise ForbiddenError("Only an administrator can export another user's data.")
    export = await personal_data.export_personal_data(
        session,
        organization_id=current_user.organization_id,
        user_id=user_id,
        actor_id=current_user.id,
        ip=client_ip,
    )
    await session.commit()
    return export


@router.post(
    "/users/{user_id}/erase",
    response_model=UserRead,
    dependencies=[Depends(require_superuser)],
    summary="Pseudonymise a person (GDPR erasure, superuser)",
)
async def erase_user(
    user_id: UUID,
    payload: EraseRequest,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> UserRead:
    user = await personal_data.erase_user(
        session,
        organization_id=current_user.organization_id,
        user_id=user_id,
        confirm_email=payload.confirm_email,
        redact_comments=payload.redact_comments,
        actor_id=current_user.id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(user)
    return UserRead.model_validate(user)
