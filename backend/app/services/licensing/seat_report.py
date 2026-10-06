"""Seat report for offline true-up (LIC-30).

Seat history is rebuilt from the audit log: every ``auth.login`` of a human
user makes that user active for the LIC-4 window, exactly as ``last_seen_at``
does at sign-in (``seats.py``). The report is not signed — an install cannot
sign anything the vendor could trust — so the licence terms back it. See
``docs/LICENSING.md`` ("Offline with a key").
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, User
from app.services.licensing.seats import ACTIVE_USER_WINDOW

#: Longest range one report covers: three years, a leap day included.
MAX_REPORT_DAYS: Final = 1096

type _Interval = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class PeriodUsage:
    """Seat use in one calendar month, clipped to the report range."""

    start: date
    end: date
    #: Distinct users active at any moment of the period.
    active_users: int
    #: The most users active at one moment, and when that was first reached.
    peak_active_users: int
    peak_at: datetime | None


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; every stored timestamp is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def months(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar months overlapping ``start``…``end`` (inclusive), clipped to it."""
    periods: list[tuple[date, date]] = []
    first = start
    while first <= end:
        next_month = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = min(end, next_month - timedelta(days=1))
        periods.append((first, last))
        first = next_month
    return periods


def active_intervals(sign_ins: Iterable[datetime]) -> list[_Interval]:
    """Merge each sign-in's active window into disjoint, sorted intervals."""
    merged: list[_Interval] = []
    for at in sorted(sign_ins):
        until = at + ACTIVE_USER_WINDOW
        if merged and at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], until))
        else:
            merged.append((at, until))
    return merged


def period_usage(
    intervals: Mapping[uuid.UUID, Sequence[_Interval]], start: date, end: date
) -> PeriodUsage:
    """Distinct and peak active users over ``start``…``end`` (inclusive days)."""
    lower, upper = _day_start(start), _day_start(end + timedelta(days=1))
    events: list[tuple[datetime, int]] = []
    users = 0
    for spans in intervals.values():
        clipped = [(max(a, lower), min(b, upper)) for a, b in spans if a < upper and b > lower]
        if clipped:
            users += 1
        for a, b in clipped:
            events.append((a, 1))
            events.append((b, -1))
    # Windows are half-open, so at a tie the one ending goes first.
    events.sort()
    current = peak = 0
    peak_at: datetime | None = None
    for at, delta in events:
        current += delta
        if current > peak:
            peak, peak_at = current, at
    return PeriodUsage(start, end, users, peak, peak_at)


async def sign_ins(
    session: AsyncSession, start: date, end: date
) -> dict[uuid.UUID, list[datetime]]:
    """Human sign-ins that leave someone active during ``start``…``end``."""
    lower = _day_start(start) - ACTIVE_USER_WINDOW
    upper = _day_start(end + timedelta(days=1))
    rows = await session.execute(
        select(AuditEvent.actor_id, AuditEvent.created_at)
        .join(User, User.id == AuditEvent.actor_id)
        .where(
            AuditEvent.action == "auth.login",
            AuditEvent.created_at >= lower,
            AuditEvent.created_at < upper,
            User.is_service.is_(False),
        )
    )
    by_user: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for actor_id, created_at in rows:
        if actor_id is not None:
            by_user[actor_id].append(_aware(created_at))
    return by_user


async def usage_by_month(session: AsyncSession, start: date, end: date) -> list[PeriodUsage]:
    """One :class:`PeriodUsage` per calendar month of the range."""
    intervals = {
        user: active_intervals(times)
        for user, times in (await sign_ins(session, start, end)).items()
    }
    return [period_usage(intervals, first, last) for first, last in months(start, end)]
