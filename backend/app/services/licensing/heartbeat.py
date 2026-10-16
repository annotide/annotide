"""The licence heartbeat (LIC-6, LIC-9, LIC-16, LIC-21).

What it sends is exactly what ``GET /licensing/telemetry/preview`` shows: the
install id, version, licence type, active user count and the salted-hash
organisation fingerprint. Never a user name, email address, annotation or
media path. The preview also lists what was withheld and why; those notes
are not sent. Weekly, on by default; ``APP_TELEMETRY_ENABLED=false`` turns it off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Connector, LicenseState, User
from app.services.licensing import seats, vendor
from app.services.licensing.fingerprint import (
    OrganizationFingerprint,
    build_fingerprint,
    email_domain,
    storage_account,
)
from app.services.licensing.state import EffectiveLicense, effective_license, get_state

HEARTBEAT_INTERVAL: Final = timedelta(days=7)

NO_SALT_NOTICE: Final = (
    "No fingerprint salt is configured, so no organisation fingerprint "
    "is built and none would be sent."
)


async def collect_email_domains(session: AsyncSession) -> list[str]:
    """Distinct email domains of active users.

    Only the domain leaves this function — the address never does. The full
    addresses are read here and discarded immediately.
    """
    addresses = await session.scalars(select(User.email).where(User.is_active.is_(True)))
    domains = {domain for address in addresses if (domain := email_domain(address))}
    return sorted(domains)


async def collect_storage_accounts(session: AsyncSession) -> list[str]:
    """Storage account or bucket names from the configured connectors.

    See :func:`storage_account`: containers and emulator endpoints never count.
    """
    configs = await session.scalars(select(Connector.config))
    accounts = {
        account
        for config in configs
        if isinstance(config, dict) and (account := storage_account(config))
    }
    return sorted(accounts)


def organization_fingerprint(
    settings: Settings, *, email_domains: list[str], storage_accounts: list[str]
) -> OrganizationFingerprint:
    """This install's fingerprint: the signals sent and the notes kept here."""
    if settings.license_fingerprint_salt:
        return build_fingerprint(
            settings.license_fingerprint_salt,
            email_domains=email_domains,
            sso_tenant_id=settings.sso_tenant_id,
            cloud_account_id=settings.cloud_account_id,
            storage_accounts=storage_accounts,
            public_hostname=settings.public_hostname,
            # The egress address is observed by the receiving server, not
            # guessed here, so it is absent from the payload by construction.
            egress_ip=None,
        )
    return OrganizationFingerprint()


def build_payload(
    settings: Settings,
    licence: EffectiveLicense,
    *,
    active_users: int,
    email_domains: list[str],
    storage_accounts: list[str],
) -> dict[str, Any]:
    """The heartbeat body, byte for byte what the preview shows (LIC-21)."""
    fingerprint = organization_fingerprint(
        settings, email_domains=email_domains, storage_accounts=storage_accounts
    )
    return {
        "install_id": settings.install_id,
        # `APP_VERSION`, baked into the release image (`backend/Dockerfile`).
        "version": settings.version,
        # The tier of the licence in force (LIC-26), expired or not.
        "licence_type": licence.edition,
        "active_users": active_users,
        "fingerprint": fingerprint.as_payload(),
    }


async def send_if_due(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
) -> bool:
    """Send the heartbeat if it is enabled and due. Returns whether it was tried.

    The caller commits.
    """
    if not settings.telemetry_enabled or not settings.license_server_url:
        return False
    moment = now or datetime.now(UTC)
    state = await get_state(session, create=True)
    assert state is not None  # create=True always returns a row
    if not vendor.is_due(
        state.heartbeat_retry_at, state.heartbeat_sent_at, HEARTBEAT_INTERVAL, moment
    ):
        return False
    licence = await effective_license(session, settings, now=moment)
    payload = build_payload(
        settings,
        licence,
        active_users=await seats.active_user_count(session, now=moment),
        email_domains=await collect_email_domains(session),
        storage_accounts=await collect_storage_accounts(session),
    )
    await _send(state, settings, client, payload, moment)
    return True


async def _send(
    state: LicenseState,
    settings: Settings,
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    state.heartbeat_attempted_at = now
    state.heartbeat_payload = payload
    try:
        await vendor.post(client, settings, "/v1/heartbeat", payload)
    except httpx.HTTPError as exc:
        state.heartbeat_error = vendor.describe(exc)
        state.heartbeat_failures += 1
        state.heartbeat_retry_at = vendor.next_attempt(exc, state.heartbeat_failures, now)
        return
    state.heartbeat_sent_at = now
    state.heartbeat_error = None
    state.heartbeat_failures = 0
    state.heartbeat_retry_at = None
