"""The 30-day Business trial (LIC-34).

An admin asks for it; the install asks the licence server for a trial key and
keeps it like a pasted one. Optional: if the server can't be reached nothing
changes (LIC-28). An expired trial key is ignored when the licence in force
is chosen (``state.resolve``), so the install is simply Community again. See
``docs/LICENSING.md`` ("Trial").
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import LicenseState
from app.services.licensing import vendor
from app.services.licensing.hosts import is_loopback
from app.services.licensing.license import (
    TRIAL,
    InvalidLicenseError,
    LicenseStatus,
    parse_and_verify,
)
from app.services.licensing.state import (
    KeySource,
    effective_license,
    get_state,
    resolve,
    store_key,
)


class TrialUnavailableError(Exception):
    """No trial for this install: a paid key, an earlier trial, or licence calls off."""


class TrialServiceError(Exception):
    """The licence server could not be reached or gave no usable trial key."""


def trial_used(state: LicenseState | None) -> bool:
    """Whether the stored key is a trial key, current or expired."""
    if state is None or not state.key:
        return False
    try:
        return parse_and_verify(state.key).tier == TRIAL
    except InvalidLicenseError:
        return False


def trial_host(settings: Settings, host: str | None) -> str | None:
    """The host a trial is bound to: `APP_PUBLIC_HOSTNAME`, else a real request host."""
    if settings.public_hostname:
        return settings.public_hostname
    return host if host is not None and not is_loopback(host) else None


async def start_trial(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    host: str | None,
    now: datetime | None = None,
) -> None:
    """Fetch and keep a trial key. The caller commits.

    Raises :class:`TrialUnavailableError` (409) or :class:`TrialServiceError` (503).
    """
    moment = now or datetime.now(UTC)
    licence = await effective_license(session, settings, now=moment, host=host)
    if licence.license is not None:
        if licence.license.tier == TRIAL:
            raise TrialUnavailableError("A trial is already running on this installation.")
        raise TrialUnavailableError("A paid licence is in force here, so there is nothing to try.")
    state = await get_state(session, create=True)
    if trial_used(state):
        raise TrialUnavailableError("This installation has already had its trial.")
    if not settings.license_refresh_enabled:
        raise TrialUnavailableError(
            "Licence calls are switched off (APP_LICENSE_REFRESH_ENABLED=false). "
            "Ask for a trial key by e-mail instead."
        )
    if not settings.license_server_url or not settings.install_id:
        raise TrialUnavailableError(
            "No licence server is configured here. Ask for a trial key by e-mail instead."
        )
    bound_to = trial_host(settings, host)
    payload = {"install_id": settings.install_id, "host": bound_to}
    try:
        response = await vendor.post(client, settings, "/v1/trial", payload)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == status.HTTP_409_CONFLICT:
            raise TrialUnavailableError(
                "This installation or its host has already had a trial."
            ) from exc
        raise TrialServiceError(vendor.describe(exc)) from exc
    except httpx.HTTPError as exc:
        raise TrialServiceError(vendor.describe(exc)) from exc
    key = _key(response)
    candidate = resolve([(KeySource.TRIAL, key)], today=licence.today, host=host)
    if (
        candidate.status is not LicenseStatus.VALID
        or candidate.license is None
        or candidate.license.tier != TRIAL
    ):
        raise TrialServiceError("The licence server returned a key that is not a usable trial.")
    await store_key(session, key, source=KeySource.TRIAL)


def _key(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError as exc:
        raise TrialServiceError("The licence server's answer is not JSON.") from exc
    key = body.get("key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key:
        raise TrialServiceError("The licence server's answer has no trial key.")
    return key
