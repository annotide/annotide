"""`/license` — this installation's licence (LIC-1, LIC-24, LIC-26, LIC-30).

Superuser-only, and never returns a key. An admin can paste a key, which is
kept in the database alongside ``APP_LICENSE_KEY``; the valid key that expires
last is the one in force. Distinct from `api/v1/licensing.py`, which covers
the telemetry preview (LIC-9, LIC-21).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from app.api.deps import (
    ClientIpDep,
    LicenceDep,
    RequestHostDep,
    SessionDep,
    SettingsDep,
    SuperuserDep,
    get_effective_license,
    require_feature,
)
from app.api.errors import (
    ConflictError,
    InvalidLicenseKeyError,
    TrialServiceError,
    TrialUnavailableError,
)
from app.schemas.license import (
    LicenseInfo,
    LicenseKeyUpdate,
    LicenseRefreshStatus,
    SeatReport,
    SeatReportPeriod,
    UsageNoticeOut,
    UsageNotices,
)
from app.services import audit
from app.services.licensing import refresh, seat_report, seats, trial, usage_notices, vendor
from app.services.licensing.features import BUSINESS_FEATURES, Feature, licensed_features
from app.services.licensing.license import LicenseStatus
from app.services.licensing.revocation import revoked_licences
from app.services.licensing.state import (
    EffectiveLicense,
    KeySource,
    clear_key,
    get_state,
    grace_ends,
    is_restricted,
    resolve,
    store_key,
)

router = APIRouter(prefix="/license", tags=["license"])


async def _active_users(session: SessionDep) -> int:
    """A dependency so tests can pin the count without a database."""
    return await seats.active_user_count(session)


ActiveUsers = Annotated[int, Depends(_active_users)]


async def _info(session: SessionDep, licence: EffectiveLicense, active_users: int) -> LicenseInfo:
    """Community mode (no key) and an invalid key both report `tier: "community"`."""
    license_ = licence.license
    limit = seats.seat_limit(licence)
    licensed = licensed_features(licence)
    return LicenseInfo(
        status=licence.status.value,
        tier=licence.edition,
        licensee=license_.licensee if license_ is not None else None,
        seats=license_.seats if license_ is not None else None,
        expires_at=license_.expires_at if license_ is not None else None,
        features=[feature for feature in BUSINESS_FEATURES if feature in licensed],
        business_features=list(BUSINESS_FEATURES),
        owner_only=not licence.keyed and limit is not None and active_users > limit,
        trial_used=trial.trial_used(await get_state(session)),
        license_id=license_.license_id if license_ is not None else None,
        source=licence.source.value if licence.source is not None else None,
        active_users=active_users,
        seat_limit=seats.seat_limit(licence),
        grace_ends_at=grace_ends(licence),
        restricted=is_restricted(licence),
        hosts=list(license_.hosts) if license_ is not None else [],
        host_mismatch=licence.host_mismatch_since is not None,
        revoked_at=licence.revoked_at,
    )


@router.get("", response_model=LicenseInfo, summary="This installation's licence status")
async def get_license(
    session: SessionDep, _admin: SuperuserDep, licence: LicenceDep, active_users: ActiveUsers
) -> LicenseInfo:
    return await _info(session, licence, active_users)


@router.get(
    "/seat-report",
    response_model=SeatReport,
    summary="Active users per month, for true-up",
    dependencies=[require_feature(Feature.SEAT_REPORT)],
)
async def get_seat_report(
    session: SessionDep,
    settings: SettingsDep,
    _admin: SuperuserDep,
    licence: LicenceDep,
    start: Annotated[date | None, Query(description="First day, UTC")] = None,
    end: Annotated[date | None, Query(description="Last day, UTC; default today")] = None,
) -> SeatReport:
    """Distinct and peak active users per calendar month, from the sign-in audit trail."""
    last = end or datetime.now(UTC).date()
    first = start or last - timedelta(days=364)
    if first > last or (last - first).days >= seat_report.MAX_REPORT_DAYS:
        raise RequestValidationError(
            [
                {
                    "loc": ("query", "start"),
                    "msg": f"start must be on or before end, at most "
                    f"{seat_report.MAX_REPORT_DAYS} days apart",
                    "type": "value_error",
                }
            ]
        )
    license_ = licence.license
    paid = license_.seats if license_ is not None else None

    def over(peak: int) -> int | None:
        return None if paid is None else max(0, peak - paid)

    usage = await seat_report.usage_by_month(session, first, last)
    peak = max((period.peak_active_users for period in usage), default=0)
    return SeatReport(
        generated_at=datetime.now(UTC),
        install_id=settings.install_id,
        license_id=license_.license_id if license_ is not None else None,
        licensee=license_.licensee if license_ is not None else None,
        tier=licence.edition,
        seats=paid,
        seat_limit=seats.seat_limit(licence),
        start=first,
        end=last,
        peak_active_users=peak,
        peak_overage=over(peak),
        periods=[
            SeatReportPeriod(
                start=period.start,
                end=period.end,
                active_users=period.active_users,
                peak_active_users=period.peak_active_users,
                peak_at=period.peak_at,
                overage=over(period.peak_active_users),
            )
            for period in usage
        ],
    )


@router.get("/usage-notices", response_model=UsageNotices, summary="Signs of seat sharing (LIC-31)")
async def get_usage_notices(
    session: SessionDep, settings: SettingsDep, _admin: SuperuserDep
) -> UsageNotices:
    """Shared accounts, parallel locks, superhuman pace, annotating service accounts.

    Computed here from the audit log; nothing is sent anywhere or blocked.
    """
    notices = await usage_notices.usage_notices(
        session, session_length=timedelta(seconds=settings.access_token_ttl)
    )
    return UsageNotices(
        window_days=usage_notices.WINDOW.days,
        notices=[
            UsageNoticeOut(
                kind=notice.kind.value,
                user_id=notice.user_id,
                email=notice.email,
                display_name=notice.display_name,
                detail=notice.detail,
                count=notice.count,
            )
            for notice in notices
        ],
    )


async def _vendor_client() -> AsyncIterator[httpx.AsyncClient]:
    """The licence-server client; tests override it with a mock transport."""
    async with vendor.open_client() as client:
        yield client


VendorClient = Annotated[httpx.AsyncClient, Depends(_vendor_client)]


async def _refresh_status(
    session: SessionDep, settings: SettingsDep, licence: EffectiveLicense
) -> LicenseRefreshStatus:
    state = await get_state(session)
    active_users = await seats.active_user_count(session)
    return LicenseRefreshStatus(
        enabled=settings.license_refresh_enabled,
        server_configured=bool(settings.license_server_url),
        payload=refresh.build_payload(settings, licence, state, active_users),
        attempted_at=state.refresh_attempted_at if state is not None else None,
        succeeded_at=state.refresh_succeeded_at if state is not None else None,
        error=state.refresh_error if state is not None else None,
        last_payload=state.refresh_payload if state is not None else None,
    )


@router.get(
    "/refresh",
    response_model=LicenseRefreshStatus,
    summary="What the licence refresh sends and how it last went",
)
async def get_refresh(
    session: SessionDep, settings: SettingsDep, _admin: SuperuserDep, licence: LicenceDep
) -> LicenseRefreshStatus:
    return await _refresh_status(session, settings, licence)


@router.post("/refresh", response_model=LicenseRefreshStatus, summary="Refresh the licence key now")
async def post_refresh(
    session: SessionDep,
    settings: SettingsDep,
    admin: SuperuserDep,
    client: VendorClient,
    client_ip: ClientIpDep,
    host: RequestHostDep,
) -> LicenseRefreshStatus:
    """Ask the licence server for a renewed key now (LIC-27). 409 when it cannot run."""
    try:
        state = await refresh.refresh(session, settings, client)
    except refresh.RefreshUnavailableError as exc:
        raise ConflictError(str(exc)) from exc
    audit.record(
        session,
        organization_id=admin.organization_id,
        actor_id=admin.id,
        action="license.refresh",
        target_type="license",
        after={"succeeded": state.refresh_error is None, "error": state.refresh_error},
        ip=client_ip,
    )
    await session.commit()
    return await _refresh_status(
        session, settings, await get_effective_license(session, settings, host)
    )


@router.put("", response_model=LicenseInfo, summary="Install a licence key")
async def put_license(
    payload: LicenseKeyUpdate,
    session: SessionDep,
    settings: SettingsDep,
    admin: SuperuserDep,
    licence: LicenceDep,
    active_users: ActiveUsers,
    client_ip: ClientIpDep,
    host: RequestHostDep,
) -> LicenseInfo:
    """Keep a pasted key in the database. Only a currently valid key is accepted.

    It replaces any earlier pasted key. ``APP_LICENSE_KEY`` stays as it is;
    whichever valid key expires last is in force. A key bound to other hosts
    than this request's is refused (LIC-29).
    """
    state = await get_state(session)
    candidate = resolve(
        [(KeySource.ADMIN, payload.key.strip())],
        today=licence.today,
        host=host,
        revoked=revoked_licences(state.revocations if state is not None else None),
    )
    if candidate.revoked_at is not None and candidate.status is LicenseStatus.EXPIRED:
        raise InvalidLicenseKeyError(
            f"This licence was revoked on {candidate.revoked_at.isoformat()}."
        )
    if candidate.host_mismatch_since is not None and candidate.license is not None:
        raise InvalidLicenseKeyError(
            f"This licence key is for {', '.join(candidate.license.hosts)}, "
            f"not {host}. Ask for a key bound to this host."
        )
    if candidate.status is LicenseStatus.EXPIRED and candidate.license is not None:
        raise InvalidLicenseKeyError(
            f"This licence key expired on {candidate.license.expires_at.isoformat()}."
        )
    if candidate.status is not LicenseStatus.VALID or candidate.license is None:
        raise InvalidLicenseKeyError(
            "This is not a valid licence key. Paste the whole key, starting with ANN1."
        )
    await store_key(session, payload.key.strip(), source=KeySource.ADMIN)
    audit.record(
        session,
        organization_id=admin.organization_id,
        actor_id=admin.id,
        action="license.update",
        target_type="license",
        after={
            "license_id": candidate.license.license_id,
            "tier": candidate.license.tier,
            "seats": candidate.license.seats,
            "expires_at": candidate.license.expires_at.isoformat(),
        },
        ip=client_ip,
    )
    await session.commit()
    return await _info(session, await get_effective_license(session, settings, host), active_users)


@router.delete("", response_model=LicenseInfo, summary="Remove the pasted licence key")
async def delete_license(
    session: SessionDep,
    settings: SettingsDep,
    admin: SuperuserDep,
    active_users: ActiveUsers,
    client_ip: ClientIpDep,
    host: RequestHostDep,
) -> LicenseInfo:
    """Forget the key kept in the database. ``APP_LICENSE_KEY`` is not affected."""
    if await clear_key(session):
        audit.record(
            session,
            organization_id=admin.organization_id,
            actor_id=admin.id,
            action="license.delete",
            target_type="license",
            ip=client_ip,
        )
        await session.commit()
    return await _info(session, await get_effective_license(session, settings, host), active_users)


@router.post("/trial", response_model=LicenseInfo, summary="Start the 30-day Business trial")
async def post_trial(
    session: SessionDep,
    settings: SettingsDep,
    admin: SuperuserDep,
    client: VendorClient,
    active_users: ActiveUsers,
    client_ip: ClientIpDep,
    host: RequestHostDep,
) -> LicenseInfo:
    """Ask the licence server for a trial key and keep it (LIC-34).

    409 `trial-unavailable` when a key is in force, this install already had
    its trial, or licence calls are off; 503 `trial-service` when the server
    can't be reached or gives no usable key.
    """
    try:
        await trial.start_trial(session, settings, client, host=host)
    except trial.TrialUnavailableError as exc:
        raise TrialUnavailableError(str(exc)) from exc
    except trial.TrialServiceError as exc:
        raise TrialServiceError(str(exc)) from exc
    licence = await get_effective_license(session, settings, host)
    audit.record(
        session,
        organization_id=admin.organization_id,
        actor_id=admin.id,
        action="license.trial",
        target_type="license",
        after={
            "license_id": licence.license.license_id if licence.license is not None else None,
            "expires_at": licence.license.expires_at.isoformat()
            if licence.license is not None
            else None,
        },
        ip=client_ip,
    )
    await session.commit()
    return await _info(session, licence, active_users)
