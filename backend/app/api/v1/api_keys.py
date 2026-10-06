"""API keys and service accounts (AUTH-4).

Both routers require a *person* behind the token: an API key can never mint,
list or revoke keys, nor create service accounts, so a leaked key cannot
propagate itself. Service-account management is superuser-only.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import ClientIpDep, PersonDep, SessionDep, require_superuser
from app.api.errors import ForbiddenError
from app.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ServiceAccountCreate,
    UserRead,
)
from app.services import api_keys

router = APIRouter(tags=["auth"])


@router.get("/api-keys", response_model=list[ApiKeyRead], summary="List API keys")
async def list_api_keys(
    session: SessionDep,
    current_user: PersonDep,
    user_id: Annotated[
        UUID | None, Query(description="Another user's keys (superuser only)")
    ] = None,
) -> list[ApiKeyRead]:
    """The caller's keys, revoked ones included; a superuser may pick any user."""
    target = current_user.id
    if user_id is not None and user_id != current_user.id:
        if not current_user.is_superuser:
            raise ForbiddenError("Only a system administrator can list another user's keys.")
        target = user_id
    rows = await api_keys.list_api_keys(
        session, organization_id=current_user.organization_id, user_id=target
    )
    return [ApiKeyRead.model_validate(row) for row in rows]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key",
)
async def create_api_key(
    payload: ApiKeyCreate,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> ApiKeyCreated:
    """Mint a key; the `token` in the response is shown exactly once.

    `user_id` lets a superuser issue a key for a service account or another
    user of the organisation. A non-superuser can only issue keys for
    themselves.
    """
    target = current_user.id
    if payload.user_id is not None and payload.user_id != current_user.id:
        if not current_user.is_superuser:
            raise ForbiddenError("Only a system administrator can issue keys for other users.")
        target = payload.user_id
    key, token = await api_keys.create_api_key(
        session,
        organization_id=current_user.organization_id,
        user_id=target,
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        actor_id=current_user.id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(key)
    return ApiKeyCreated(**ApiKeyRead.model_validate(key).model_dump(), token=token)


@router.delete(
    "/api-keys/{key_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_api_key(
    key_id: UUID,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> Response:
    """Revoke a key. Idempotent: revoking twice is still 204."""
    await api_keys.revoke_api_key(
        session,
        organization_id=current_user.organization_id,
        key_id=key_id,
        actor_id=current_user.id,
        actor_is_superuser=current_user.is_superuser,
        ip=client_ip,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Service accounts -------------------------------------------------------

_superuser = [Depends(require_superuser)]


@router.get(
    "/service-accounts",
    response_model=list[UserRead],
    dependencies=_superuser,
    summary="List service accounts (superuser)",
)
async def list_service_accounts(session: SessionDep, current_user: PersonDep) -> list[UserRead]:
    rows = await api_keys.list_service_accounts(
        session, organization_id=current_user.organization_id
    )
    return [UserRead.model_validate(row) for row in rows]


@router.post(
    "/service-accounts",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_superuser,
    summary="Create a service account (superuser)",
)
async def create_service_account(
    payload: ServiceAccountCreate,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> UserRead:
    """A user row that cannot sign in; give it project memberships and a key."""
    user = await api_keys.create_service_account(
        session,
        organization_id=current_user.organization_id,
        display_name=payload.display_name,
        actor_id=current_user.id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(user)
    return UserRead.model_validate(user)


@router.delete(
    "/service-accounts/{user_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_superuser,
    summary="Deactivate a service account and revoke its keys (superuser)",
)
async def delete_service_account(
    user_id: UUID,
    session: SessionDep,
    current_user: PersonDep,
    client_ip: ClientIpDep,
) -> Response:
    await api_keys.delete_service_account(
        session,
        organization_id=current_user.organization_id,
        user_id=user_id,
        actor_id=current_user.id,
        ip=client_ip,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
