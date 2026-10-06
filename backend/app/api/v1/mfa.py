"""`/auth/mfa` — TOTP multi-factor authentication for local accounts (AUTH-2).

Enrolment is two steps: `setup` stores a pending seed and returns it for the
authenticator app, `enable` confirms it with a code and returns the recovery
codes once. Sign-in then asks for a code (`POST /auth/login` with `otp`).
API keys never reach these routes. See `services/mfa.py`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app.api.deps import ClientIpDep, PersonDep, SessionDep, SettingsDep
from app.api.errors import (
    ConflictError,
    ForbiddenError,
    MfaCodeRejectedError,
    NotFoundError,
)
from app.models import User
from app.schemas import MfaCode, MfaRecoveryCodes, MfaSetup, MfaStatus
from app.services import audit, mfa

router = APIRouter(prefix="/auth/mfa", tags=["auth"])


async def _user(session: SessionDep, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    return user


def _status(user: User) -> MfaStatus:
    return MfaStatus(
        enabled=mfa.is_enabled(user),
        pending=not mfa.is_enabled(user) and user.totp_secret is not None,
        recovery_codes_left=len(user.mfa_recovery_codes or []),
        available=mfa.is_available(user),
    )


@router.get("", response_model=MfaStatus, summary="The caller's MFA state")
async def get_mfa(session: SessionDep, person: PersonDep) -> MfaStatus:
    return _status(await _user(session, person.id))


@router.post("/setup", response_model=MfaSetup, summary="Start adding an authenticator app")
async def setup(session: SessionDep, person: PersonDep) -> MfaSetup:
    """A new seed, pending until `enable` confirms it. Replaces an unconfirmed one."""
    user = await _user(session, person.id)
    try:
        secret = mfa.begin_setup(user)
    except mfa.MfaError as exc:
        raise ConflictError(str(exc)) from exc
    await session.commit()
    return MfaSetup(secret=secret, otpauth_uri=mfa.otpauth_uri(secret, user.email))


@router.post("/enable", response_model=MfaRecoveryCodes, summary="Confirm and turn MFA on")
async def enable(
    payload: MfaCode, session: SessionDep, person: PersonDep, client_ip: ClientIpDep
) -> MfaRecoveryCodes:
    user = await _user(session, person.id)
    try:
        codes = mfa.enable(user, payload.code)
    except mfa.MfaError as exc:
        raise ConflictError(str(exc)) from exc
    if codes is None:
        raise MfaCodeRejectedError(
            "That code does not match. Check the time on the phone and try the next code."
        )
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.mfa_enable",
        target_type="user",
        target_id=user.id,
        ip=client_ip,
    )
    await session.commit()
    return MfaRecoveryCodes(recovery_codes=codes)


@router.post(
    "/recovery-codes", response_model=MfaRecoveryCodes, summary="Replace the recovery codes"
)
async def recovery_codes(
    payload: MfaCode, session: SessionDep, person: PersonDep
) -> MfaRecoveryCodes:
    """Needs a current code from the app, not a recovery code."""
    user = await _user(session, person.id)
    if not mfa.is_enabled(user):
        raise ConflictError("MFA is not on for this account.")
    try:
        accepted = mfa.verify_totp(user, payload.code)
    except mfa.MfaError as exc:
        raise ConflictError(str(exc)) from exc
    if not accepted:
        raise MfaCodeRejectedError("That code does not match or was already used.")
    codes = mfa.replace_recovery_codes(user)
    await session.commit()
    return MfaRecoveryCodes(recovery_codes=codes)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="Turn MFA off")
async def disable(
    session: SessionDep,
    person: PersonDep,
    client_ip: ClientIpDep,
    settings: SettingsDep,
    payload: MfaCode | None = None,
    user_id: Annotated[UUID | None, Query(description="Superuser: reset another account")] = None,
) -> Response:
    """With a code for one's own account; a superuser resets another without one."""
    if user_id is not None and user_id != person.id:
        if not person.is_superuser:
            raise ForbiddenError("Only an administrator can reset another account's MFA.")
        user = await session.scalar(
            select(User).where(User.id == user_id, User.organization_id == person.organization_id)
        )
        if user is None:
            raise NotFoundError("User not found.")
    else:
        user = await _user(session, person.id)
        if settings.mfa_required_for_admins and user.is_superuser:
            # Another administrator can still reset it (a lost phone).
            raise ForbiddenError("MFA is required for administrators on this installation.")
        if not mfa.is_enabled(user):
            raise ConflictError("MFA is not on for this account.")
        if payload is None or not mfa.verify(user, payload.code):
            raise MfaCodeRejectedError("That code does not match or was already used.")
    mfa.disable(user)
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=person.id,
        action="auth.mfa_disable",
        target_type="user",
        target_id=user.id,
        ip=client_ip,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
