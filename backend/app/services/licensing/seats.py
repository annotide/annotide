"""Seat counting and the sign-in seat check (LIC-23, LIC-24).

An *active user* is an active, non-service user seen within the LIC-4 window.
The check runs where a user becomes active — at sign-in — so a user who is
already active is never refused and nobody is cut off mid-session. See
``docs/LICENSING.md`` ("Enforcement without a vendor dependency").

Enforcement is inert while the build carries no vendor public keys: such a
build cannot hold a valid key at all, so limiting it would cap every install,
the vendor's own included, at three users.

In Community mode with more users active than the limit (after a trial or a
lapsed key), only the owner may sign in until users are deactivated (LIC-36):
otherwise a trial's users would stay active for good.

Two sign-ins racing for the last seat can both pass. That is one seat of
overage at worst, well inside the soft margin, and not worth a lock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services.licensing.state import EffectiveLicense

#: The LIC-4 active-user window.
ACTIVE_USER_WINDOW: Final = timedelta(days=30)
#: Community is up to three people (LIC-23), with no overage.
COMMUNITY_SEATS: Final = 3

COMMUNITY_REFUSAL: Final = (
    "This installation runs the free Community edition, which is for up to three "
    "people. Another user can sign in once one of them has been inactive for 30 days, "
    "or an administrator can add users with a Team or Business licence."
)
OWNER_ONLY_REFUSAL: Final = (
    "This installation runs the free Community edition, for up to three people, and "
    "more users than that are active. Until the owner deactivates users down to three, "
    "only the owner can sign in."
)
SEATS_REFUSAL: Final = (
    "Every licensed seat on this installation is in use. Ask an administrator "
    "to add seats; a seat frees up 30 days after its user was last active."
)


def seat_limit(licence: EffectiveLicense) -> int | None:
    """How many users may be active at once, overage included.

    ``None`` means no limit is enforced (a build without vendor keys). A valid
    or expired key allows its seats plus 10 %, at least one; expiry itself is
    LIC-5's business, not this check's. No key or an invalid one is Community.
    """
    if not licence.enforcing:
        return None
    if licence.license is None:
        return COMMUNITY_SEATS
    seats = licence.license.seats
    return seats + max(1, seats // 10)


async def active_user_count(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Distinct human users seen within the active-user window."""
    cutoff = (now or datetime.now(UTC)) - ACTIVE_USER_WINDOW
    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.is_active.is_(True),
            User.is_service.is_(False),
            User.last_seen_at >= cutoff,
        )
    )
    return int(count or 0)


async def may_sign_in(
    session: AsyncSession,
    user: User,
    licence: EffectiveLicense,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether ``user`` may sign in without exceeding the seat limit."""
    limit = seat_limit(licence)
    if limit is None or user.is_service:
        return True
    now = now or datetime.now(UTC)
    if not licence.keyed and await owner_only(session, licence, now=now):
        return await is_owner(session, user)
    if user.last_seen_at is not None and _aware(user.last_seen_at) >= now - ACTIVE_USER_WINDOW:
        return True
    # An administrator must always be able to get in and sort out the seats.
    # Community exempts only the owner: exempting every superuser would make
    # "everyone is a superuser" a one-click way around the limit.
    if user.is_superuser and (licence.keyed or await is_owner(session, user)):
        return True
    return await active_user_count(session, now=now) < limit


async def owner_only(
    session: AsyncSession, licence: EffectiveLicense, *, now: datetime | None = None
) -> bool:
    """Community with more users active than its limit: only the owner signs in (LIC-36)."""
    limit = seat_limit(licence)
    if limit is None or licence.keyed:
        return False
    return await active_user_count(session, now=now) > limit


async def is_owner(session: AsyncSession, user: User) -> bool:
    """Whether ``user`` is the owner: the non-service superuser created first."""
    owner_id = await session.scalar(
        select(User.id)
        .where(User.is_superuser.is_(True), User.is_service.is_(False))
        .order_by(User.created_at, User.id)
        .limit(1)
    )
    return owner_id == user.id


async def refusal_message(
    session: AsyncSession, licence: EffectiveLicense, *, now: datetime | None = None
) -> str:
    """What the refused user is told."""
    if licence.keyed:
        return SEATS_REFUSAL
    if await owner_only(session, licence, now=now):
        return OWNER_ONLY_REFUSAL
    return COMMUNITY_REFUSAL


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; every stored one is UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
