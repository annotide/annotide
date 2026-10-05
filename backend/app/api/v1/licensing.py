"""Licence telemetry transparency (LIC-9, LIC-21) and the organisational-use notice (LIC-20).

The admin can see exactly what this installation would send home, byte for byte,
before it is sent — and what was deliberately left out, and why. That is the
whole point: telemetry people cannot inspect is telemetry people are right to
distrust.

Nothing here reports annotations, media, item paths, user names or email
addresses. See ``docs/LICENSING.md`` for the design and the reasoning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import LicenceDep, SessionDep, SettingsDep, SuperuserDep
from app.models import LicenseState
from app.services.licensing import email_domain, heartbeat, is_public_email_domain, seats
from app.services.licensing.state import get_state

router = APIRouter(prefix="/licensing", tags=["licensing"])


class TelemetryPreview(BaseModel):
    """Exactly what the heartbeat would send, plus what was withheld and what was sent."""

    enabled: bool
    install_id: str | None
    version: str
    licence_type: str
    active_users: int
    fingerprint: dict[str, Any]
    #: What the fingerprint left out and why. Shown here only, never sent: a
    #: note can name an internal host or domain in the clear (LIC-17, LIC-21).
    withheld: list[str] = []
    #: Present when telemetry cannot be built at all, explaining why.
    notice: str | None = None
    #: Whether `APP_LICENSE_SERVER_URL` is set; without it nothing is sent.
    server_configured: bool = False
    attempted_at: datetime | None = None
    sent_at: datetime | None = None
    error: str | None = None
    #: The body of the last heartbeat, as sent (LIC-21).
    last_payload: dict[str, Any] | None = None


class OrganizationalUseNotice(BaseModel):
    """Whether this Community install looks like it is being used by an organisation."""

    looks_organizational: bool
    reasons: list[str]
    message: str | None


async def _active_user_count(session: SessionDep) -> int:
    """Users seen within the LIC-4 window — the same count the seat check uses."""
    return await seats.active_user_count(session)


async def _collect_email_domains(session: SessionDep) -> list[str]:
    """Distinct email domains of active users; the addresses never leave the service."""
    return await heartbeat.collect_email_domains(session)


async def _collect_storage_accounts(session: SessionDep) -> list[str]:
    """Storage account or bucket names from the configured connectors."""
    return await heartbeat.collect_storage_accounts(session)


async def _licence_state(session: SessionDep) -> LicenseState | None:
    """The heartbeat's last attempt; a dependency so tests need no database."""
    return await get_state(session)


ActiveUserCount = Annotated[int, Depends(_active_user_count)]
EmailDomains = Annotated[list[str], Depends(_collect_email_domains)]
StorageAccounts = Annotated[list[str], Depends(_collect_storage_accounts)]
LicenceState = Annotated[LicenseState | None, Depends(_licence_state)]


@router.get(
    "/telemetry/preview",
    response_model=TelemetryPreview,
    summary="Exactly what this installation would send home",
)
async def telemetry_preview(
    settings: SettingsDep,
    _admin: SuperuserDep,
    licence: LicenceDep,
    active_users: ActiveUserCount,
    email_domains: EmailDomains,
    storage_accounts: StorageAccounts,
    state: LicenceState,
) -> TelemetryPreview:
    """Show the heartbeat payload verbatim, including the withheld signals (LIC-21).

    Works whether or not telemetry is enabled, so an admin can inspect what
    *would* be sent before deciding to turn it on, and what was last sent.
    """
    payload = heartbeat.build_payload(
        settings,
        licence,
        active_users=active_users,
        email_domains=email_domains,
        storage_accounts=storage_accounts,
    )
    fingerprint = heartbeat.organization_fingerprint(
        settings, email_domains=email_domains, storage_accounts=storage_accounts
    )
    if not settings.license_fingerprint_salt:
        notice: str | None = heartbeat.NO_SALT_NOTICE
    elif not settings.telemetry_enabled:
        notice = "Telemetry is disabled; nothing is sent."
    else:
        notice = None
    return TelemetryPreview(
        enabled=settings.telemetry_enabled,
        notice=notice,
        server_configured=bool(settings.license_server_url),
        attempted_at=state.heartbeat_attempted_at if state is not None else None,
        sent_at=state.heartbeat_sent_at if state is not None else None,
        error=state.heartbeat_error if state is not None else None,
        last_payload=state.heartbeat_payload if state is not None else None,
        withheld=list(fingerprint.excluded),
        **payload,
    )


@router.get(
    "/organizational-use",
    response_model=OrganizationalUseNotice,
    summary="Whether this Community install looks like organisational use",
)
async def organizational_use(
    settings: SettingsDep,
    _admin: SuperuserDep,
    active_users: ActiveUserCount,
    email_domains: EmailDomains,
) -> OrganizationalUseNotice:
    """The honesty prompt (LIC-20).

    Runs entirely locally — no telemetry, no network — and simply tells the
    admin what the installation can already see about itself. Most people
    comply once told plainly, which is why this is expected to convert better
    than any clustering the vendor does.
    """
    reasons: list[str] = []

    company_domains = [
        domain
        for raw in email_domains
        if (domain := email_domain(raw) if "@" in raw else raw)
        and not is_public_email_domain(domain)
    ]
    if company_domains:
        reasons.append(f"{len(company_domains)} user account(s) use a non-personal email domain.")

    if settings.sso_tenant_id:
        reasons.append("A single sign-on provider is configured.")

    if settings.cloud_account_id:
        reasons.append("The deployment runs under an organisational cloud account.")

    if active_users >= seats.COMMUNITY_SEATS:
        reasons.append(
            f"{active_users} users have been active in the last 30 days; Community is for "
            f"up to {seats.COMMUNITY_SEATS}."
        )

    if not reasons:
        return OrganizationalUseNotice(looks_organizational=False, reasons=[], message=None)

    return OrganizationalUseNotice(
        looks_organizational=True,
        reasons=reasons,
        message=(
            "This installation runs the free Community edition, for up to three people. "
            "It looks like a team is using it: Team adds people (12 €/user/month, or "
            "29 €/month for five plus 12 € per extra user), Business adds single sign-on, "
            "SCIM, folder permissions and quality control. Try everything free for 30 days."
        ),
    )
