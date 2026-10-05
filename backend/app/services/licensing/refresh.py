"""The daily licence refresh (LIC-27).

A keyed install tells the licence server which licence it holds, where it
runs and how many seats are in use, and gets the renewed key back if one
exists. That is how a paid key renews without anyone touching the install.
It is licence accounting, not telemetry: no fingerprint, no user identifiers.
On by default; failing or being switched off never changes behaviour. See
``docs/LICENSING.md`` ("Licence refresh and heartbeat").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import LicenseState
from app.services.licensing import revocation, seats, vendor
from app.services.licensing.heartbeat import VERSION, is_due
from app.services.licensing.license import InvalidLicenseError, LicenseStatus, license_status
from app.services.licensing.state import (
    EffectiveLicense,
    KeySource,
    effective_license,
    get_state,
    resolve,
    store_key,
)

REFRESH_INTERVAL: Final = timedelta(hours=24)


class RefreshUnavailableError(Exception):
    """Refresh cannot run here: switched off, no server, or no key to refresh."""


class _RejectedKeyError(Exception):
    """The licence server answered with something that is not a usable key."""


def unavailable_reason(settings: Settings, licence: EffectiveLicense) -> str | None:
    """Why a refresh cannot run now, or ``None`` when it can."""
    if not settings.license_refresh_enabled:
        return "Licence refresh is switched off (APP_LICENSE_REFRESH_ENABLED=false)."
    if not settings.license_server_url:
        return "No licence server is configured (APP_LICENSE_SERVER_URL)."
    if licence.license is None:
        return "No verified licence key is in force, so there is nothing to refresh."
    return None


def build_payload(
    settings: Settings, licence: EffectiveLicense, state: LicenseState | None, active_users: int
) -> dict[str, Any] | None:
    """What the refresh sends; ``None`` without a verified key."""
    if licence.license is None:
        return None
    return {
        "license_id": licence.license.license_id,
        "install_id": settings.install_id,
        "host": settings.public_hostname or (state.last_host if state is not None else None),
        "active_users": active_users,
        "version": VERSION,
    }


async def refresh(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
) -> LicenseState:
    """Refresh now, due or not. The caller commits.

    Raises :class:`RefreshUnavailableError` when it cannot run at all; a failed
    call is recorded on the returned state instead.
    """
    moment = now or datetime.now(UTC)
    licence = await effective_license(session, settings, now=moment)
    reason = unavailable_reason(settings, licence)
    if reason is not None:
        raise RefreshUnavailableError(reason)
    state = await get_state(session, create=True)
    assert state is not None  # create=True always returns a row
    active_users = await seats.active_user_count(session, now=moment)
    payload = build_payload(settings, licence, state, active_users)
    assert payload is not None  # a verified key is in force
    state.refresh_attempted_at = moment
    state.refresh_payload = payload
    try:
        response = await vendor.post(client, settings, "/v1/refresh", payload)
        key, revocations = _answer(response)
        if revocations is not None:
            _accept_revocations(state, revocations)
        if key is not None:
            await _accept(session, state, key, today=licence.today, host=payload["host"])
    except (httpx.HTTPError, _RejectedKeyError) as exc:
        state.refresh_error = vendor.describe(exc)
        return state
    state.refresh_succeeded_at = moment
    state.refresh_error = None
    return state


async def refresh_if_due(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
) -> bool:
    """The worker's daily refresh, retried hourly after a failure. The caller commits."""
    if not settings.license_refresh_enabled or not settings.license_server_url:
        return False
    moment = now or datetime.now(UTC)
    state = await get_state(session)
    if state is not None and not is_due(
        state.refresh_attempted_at, state.refresh_succeeded_at, REFRESH_INTERVAL, moment
    ):
        return False
    try:
        await refresh(session, settings, client, now=moment)
    except RefreshUnavailableError:
        return False
    return True


def _answer(response: httpx.Response) -> tuple[str | None, str | None]:
    """The key and the revocation list in the server's answer; either may be absent."""
    try:
        body = response.json()
    except ValueError as exc:
        raise _RejectedKeyError("The licence server's answer is not JSON.") from exc
    if not isinstance(body, dict):
        raise _RejectedKeyError("The licence server's answer has no usable key.")
    key, revocations = body.get("key"), body.get("revocations")
    if key is not None and not isinstance(key, str):
        raise _RejectedKeyError("The licence server's answer has no usable key.")
    if revocations is not None and not isinstance(revocations, str):
        raise _RejectedKeyError("The licence server's revocation list is not usable.")
    return key or None, revocations or None


def _accept_revocations(state: LicenseState, token: str) -> None:
    """Keep a verified revocation list (LIC-8) that is newer than the stored one."""
    try:
        incoming = revocation.parse_and_verify(token)
    except InvalidLicenseError as exc:
        raise _RejectedKeyError(
            "The licence server returned a revocation list that does not verify."
        ) from exc
    if state.revocations:
        try:
            stored = revocation.parse_and_verify(state.revocations)
        except InvalidLicenseError:
            stored = None
        if stored is not None and stored.issued_at >= incoming.issued_at:
            return
    state.revocations = token


async def _accept(
    session: AsyncSession, state: LicenseState, key: str, *, today: date, host: str | None
) -> None:
    """Keep ``key`` if it is valid here and outlasts the stored one."""
    candidate = resolve(
        [(KeySource.REFRESH, key)],
        today=today,
        host=host,
        revoked=revocation.revoked_licences(state.revocations),
    )
    if candidate.status is not LicenseStatus.VALID or candidate.license is None:
        raise _RejectedKeyError(
            "The licence server returned a key that is not valid for this installation."
        )
    _, stored = license_status(state.key, today=today)
    if stored is None or candidate.license.expires_at > stored.expires_at:
        await store_key(session, key, source=KeySource.REFRESH)
