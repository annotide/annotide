"""Response DTO for `GET /license` (LIC-1).

Deliberately never carries the licence key itself — only what it grants.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class LicenseInfo(BaseSchema):
    """Licence status as seen by an administrator.

    ``tier`` is the edition (LIC-32): without a key in force (or with an
    invalid one) it is ``"community"`` and the key's fields are ``None``.
    ``seat_limit`` includes the overage and is ``None`` when the build
    enforces no limit (LIC-23, LIC-24).
    """

    status: str
    tier: str
    licensee: str | None
    seats: int | None
    expires_at: date | None
    #: The Business features the licence in force unlocks (LIC-33).
    features: list[str]
    #: Every Business feature id, so a client can show the locked ones.
    business_features: list[str]
    #: Community with more users active than its limit: only the owner signs in (LIC-36).
    owner_only: bool
    #: The stored key is a trial key, current or expired (LIC-34).
    trial_used: bool
    license_id: str | None
    #: Where the licence in force came from: ``env``, ``admin``, ``refresh`` or ``trial``.
    source: str | None
    active_users: int
    seat_limit: int | None
    #: Last day of the LIC-5 grace period after ``expires_at`` (or after the
    #: host mismatch began, LIC-29); ``None`` without a key.
    grace_ends_at: date | None
    #: Restricted mode: annotation and new tasks are refused (LIC-5).
    restricted: bool
    #: Hosts the key is bound to (LIC-29); empty when unbound or without a key.
    hosts: list[str]
    #: The request arrived on a host outside ``hosts``: the expired path.
    host_mismatch: bool
    #: The licence was revoked (LIC-8); its last valid day.
    revoked_at: date | None


class SeatReportPeriod(BaseSchema):
    """Seat use in one calendar month, clipped to the report range (LIC-30)."""

    start: date
    end: date
    #: Distinct users active at any moment of the period.
    active_users: int
    #: The most users active at one moment; ``peak_at`` is ``None`` when zero.
    peak_active_users: int
    peak_at: datetime | None
    #: ``peak_active_users`` over ``seats``; ``None`` without a key.
    overage: int | None


class SeatReport(BaseSchema):
    """`GET /license/seat-report`: distinct active users per month for true-up (LIC-30).

    Overage is measured against the licence in force now. Not signed; the
    licence terms back it.
    """

    generated_at: datetime
    install_id: str | None
    license_id: str | None
    licensee: str | None
    tier: str
    seats: int | None
    seat_limit: int | None
    start: date
    end: date
    peak_active_users: int
    peak_overage: int | None
    periods: list[SeatReportPeriod]


class LicenseRefreshStatus(BaseSchema):
    """`GET` / `POST /license/refresh` (LIC-27): what is sent, and how the last try went."""

    #: `APP_LICENSE_REFRESH_ENABLED`.
    enabled: bool
    #: Whether `APP_LICENSE_SERVER_URL` is set; without it nothing is sent.
    server_configured: bool
    #: What a refresh would send now; ``None`` without a verified key.
    payload: dict[str, Any] | None
    attempted_at: datetime | None
    succeeded_at: datetime | None
    error: str | None
    #: The body of the last refresh, as sent.
    last_payload: dict[str, Any] | None


class UsageNoticeOut(BaseSchema):
    """One sign of seat sharing (LIC-31). A notice for the admin, never a block."""

    #: `parallel_sign_ins`, `parallel_tasks`, `superhuman_pace` or
    #: `service_account_annotating`.
    kind: str
    user_id: UUID
    email: str
    display_name: str
    detail: str
    count: int


class UsageNotices(BaseSchema):
    """`GET /license/usage-notices`."""

    window_days: int
    notices: list[UsageNoticeOut]


class LicenseKeyUpdate(BaseSchema):
    """`PUT /license` body: a key to keep in the database (LIC-26)."""

    key: str = Field(min_length=1, max_length=8192)
