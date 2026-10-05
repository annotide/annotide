"""Which licence is in force, the clock guard, host binding and restricted mode.

LIC-5, LIC-8, LIC-25, LIC-26, LIC-29.

Keys come from two places: ``APP_LICENSE_KEY`` and the ``license_state`` row
(pasted by an admin, or fetched by the licence refresh, LIC-27). The valid key
that expires last wins; failing that, the expired one that expired last. Only
one licence is ever in force, so seats never add up across keys.

Expiry is evaluated against the later of today and the latest date the
install has recorded at sign-in, so winding the clock back does not revive
an expired key. A key bound to other hosts than the one a request arrives on
takes the expired path, its grace counted from the first sign-in on such a
host, and so does a revoked key from its revocation date (LIC-8). See
``docs/LICENSING.md`` ("Keys: validity, delivery, renewal", "Binding a key
to its host").
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import LicenseState
from app.models.licensing import INSTALL_SLOT
from app.services.licensing.hosts import host_allowed, is_loopback
from app.services.licensing.keys import VENDOR_PUBLIC_KEYS
from app.services.licensing.license import TRIAL, License, LicenseStatus, license_status
from app.services.licensing.revocation import revoked_licences

#: The edition of an install without a key in force (LIC-32).
COMMUNITY: Final = "community"


class KeySource(enum.StrEnum):
    """Where the licence in force came from."""

    ENV = "env"
    ADMIN = "admin"
    REFRESH = "refresh"
    TRIAL = "trial"


@dataclass(frozen=True)
class EffectiveLicense:
    """The licence in force, resolved from every key source."""

    status: LicenseStatus
    license: License | None
    source: KeySource | None
    #: Whether the build carries vendor keys. Without them no key can be
    #: valid, so no limit is enforced (see ``seats.py``).
    enforcing: bool
    #: The date expiry was evaluated against (never earlier than the clock guard).
    today: date
    #: Set when the key in force is bound to other hosts than the request's
    #: (LIC-29): the day the mismatch was first seen. Grace counts from here.
    host_mismatch_since: date | None = None
    #: The key in force was revoked (LIC-8); its last valid day.
    revoked_at: date | None = None

    @property
    def keyed(self) -> bool:
        """A verified key, valid or expired — as opposed to Community or invalid."""
        return self.license is not None

    @property
    def edition(self) -> str:
        """`community` without a key in force, else the key's tier (LIC-32)."""
        return self.license.tier if self.license is not None else COMMUNITY


@dataclass(frozen=True, slots=True)
class _Verified:
    status: LicenseStatus
    license: License
    source: KeySource
    #: Its last valid day, all things considered.
    ended: date
    mismatch_since: date | None
    revoked_at: date | None


def resolve(
    candidates: Sequence[tuple[KeySource, str | None]],
    *,
    today: date,
    public_keys: Mapping[str, bytes] | None = None,
    host: str | None = None,
    mismatch_since: date | None = None,
    revoked: Mapping[str, date] | None = None,
) -> EffectiveLicense:
    """Pick the licence in force from ``(source, key)`` pairs; earlier wins ties.

    A key's last valid day is the earliest of its ``expires_at``, its
    revocation date in ``revoked`` (LIC-8) and — when it is bound to hosts
    that exclude ``host`` — ``mismatch_since`` (today when unrecorded).
    """
    keys = VENDOR_PUBLIC_KEYS if public_keys is None else public_keys
    since = mismatch_since or today
    verified: list[_Verified] = []
    any_key = False
    for source, key in candidates:
        if not key:
            continue
        _status, license_ = license_status(key, today=today, public_keys=keys)
        if license_ is None:
            any_key = True
            continue
        ended = license_.expires_at
        revoked_at = (revoked or {}).get(license_.license_id)
        if revoked_at is not None:
            ended = min(ended, revoked_at)
        mismatch = None if host_allowed(license_.hosts, host) else since
        if mismatch is not None:
            ended = min(ended, mismatch)
        status = LicenseStatus.EXPIRED if ended < today or mismatch else LicenseStatus.VALID
        if status is LicenseStatus.EXPIRED and license_.tier == TRIAL:
            # An ended trial was never paid for: no grace, no restricted
            # mode, just Community again (LIC-34). It counts as no key.
            continue
        any_key = True
        verified.append(_Verified(status, license_, source, ended, mismatch, revoked_at))
    for wanted in (LicenseStatus.VALID, LicenseStatus.EXPIRED):
        matches = [entry for entry in verified if entry.status is wanted]
        if matches:
            best = max(matches, key=lambda entry: entry.ended)
            return EffectiveLicense(
                best.status,
                best.license,
                best.source,
                bool(keys),
                today,
                best.mismatch_since,
                best.revoked_at,
            )
    status = LicenseStatus.INVALID if any_key else LicenseStatus.COMMUNITY
    return EffectiveLicense(status, None, None, bool(keys), today)


#: How long an expired licence keeps working before restricted mode (LIC-5).
GRACE_PERIOD: Final = timedelta(days=30)


def grace_ends(licence: EffectiveLicense) -> date | None:
    """The last day of the LIC-5 grace period, for a verified key.

    For a key bound to other hosts (LIC-29) the period starts at the
    mismatch, so moving to a new domain gives 30 days to get a new key.
    """
    if licence.license is None:
        return None
    ended = licence.license.expires_at
    for cut_short in (licence.host_mismatch_since, licence.revoked_at):
        if cut_short is not None:
            ended = min(ended, cut_short)
    return ended + GRACE_PERIOD


def is_restricted(licence: EffectiveLicense) -> bool:
    """Restricted mode (LIC-5): an expired key whose grace period is over.

    Annotation and new tasks stop; reading and export never do. Community and
    an invalid key are never restricted — they are limited by seats instead.
    """
    last_day = grace_ends(licence)
    return (
        licence.status is LicenseStatus.EXPIRED
        and last_day is not None
        and licence.today > last_day
    )


async def get_state(session: AsyncSession, *, create: bool = False) -> LicenseState | None:
    """The install's one ``license_state`` row.

    Migration 0010 inserts it, so ``create`` only matters for a database built
    without migrations (the tests).
    """
    row = await session.scalar(select(LicenseState).where(LicenseState.slot == INSTALL_SLOT))
    if row is None and create:
        row = LicenseState(slot=INSTALL_SLOT)
        session.add(row)
    return row


async def effective_license(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    host: str | None = None,
    record: bool = False,
    public_keys: Mapping[str, bytes] | None = None,
) -> EffectiveLicense:
    """Resolve the licence in force for a request on ``host``. The caller commits.

    ``record`` advances the clock high-water mark to today and starts or
    clears the host-mismatch clock (LIC-29). Sign-in records; read-only
    endpoints do not, so a GET never writes.
    """
    today = (now or datetime.now(UTC)).date()
    keys = VENDOR_PUBLIC_KEYS if public_keys is None else public_keys
    if not keys:
        # Nothing can be verified, so nothing is enforced: skip the database.
        return resolve([(KeySource.ENV, settings.license_key)], today=today, public_keys=keys)
    state = await get_state(session, create=record)
    high_water = state.clock_high_water if state is not None else None
    if record and state is not None and (high_water is None or today > high_water):
        state.clock_high_water = today
    effective_today = max(today, high_water) if high_water is not None else today

    candidates: list[tuple[KeySource, str | None]] = [(KeySource.ENV, settings.license_key)]
    if state is not None and state.key:
        candidates.append((KeySource(state.key_source or KeySource.ADMIN), state.key))
    since = state.host_mismatch_since if state is not None else None
    licence = resolve(
        candidates,
        today=effective_today,
        public_keys=keys,
        host=host,
        mismatch_since=since,
        revoked=revoked_licences(
            state.revocations if state is not None else None, public_keys=keys
        ),
    )
    if record and state is not None:
        if host is not None and not is_loopback(host):
            state.last_host = host  # what the licence refresh reports (LIC-27)
        if licence.host_mismatch_since is not None:
            state.host_mismatch_since = licence.host_mismatch_since
        elif licence.keyed and host is not None and not is_loopback(host):
            # Only a real matching host ends the mismatch: loopback always
            # matches, and signing in there must not restart the clock.
            state.host_mismatch_since = None
    return licence


async def store_key(session: AsyncSession, key: str, *, source: KeySource) -> None:
    """Keep ``key`` as the database key, replacing any earlier one. The caller commits.

    The caller checks the key first; this stores whatever it is given.
    """
    state = await get_state(session, create=True)
    assert state is not None  # create=True always returns a row
    state.key = key
    state.key_source = source.value


async def clear_key(session: AsyncSession) -> bool:
    """Forget the database key. ``APP_LICENSE_KEY`` is untouched. The caller commits."""
    state = await get_state(session)
    if state is None or state.key is None:
        return False
    state.key = None
    state.key_source = None
    return True
