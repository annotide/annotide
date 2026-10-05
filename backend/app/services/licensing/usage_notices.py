"""Signs of seat sharing, shown to the admin as notices (LIC-31).

Account sharing, parallel task claims, faster-than-human throughput and
service accounts doing annotation work are all things the licence terms
count as seats, and none of them can be told apart from legitimate use with
certainty. So they are notices for the admin, computed locally from the audit
log and the live task locks, never sent anywhere and never a block. See
``docs/LICENSING.md`` ("Enforcement without a vendor dependency").
"""

from __future__ import annotations

import ipaddress
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Task, TaskStatus, User

WINDOW: Final = timedelta(days=30)
PARALLEL_SIGN_IN_DAYS: Final = 3
PARALLEL_TASK_LOCKS: Final = 3
#: One submit every three seconds, sustained for an hour.
SUPERHUMAN_PER_HOUR: Final = 1200
SERVICE_ANNOTATION_DAYS: Final = 5


class NoticeKind(StrEnum):
    PARALLEL_SIGN_INS = "parallel_sign_ins"
    PARALLEL_TASKS = "parallel_tasks"
    SUPERHUMAN_PACE = "superhuman_pace"
    SERVICE_ACCOUNT_ANNOTATING = "service_account_annotating"


@dataclass(frozen=True, slots=True)
class UsageNotice:
    kind: NoticeKind
    user_id: uuid.UUID
    email: str
    display_name: str
    detail: str
    count: int


type _Events = Mapping[uuid.UUID, Sequence[tuple[datetime, str | None]]]


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; every stored timestamp is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def network(ip: str | None) -> str | None:
    """The ``/24`` (IPv6 ``/48``) an address belongs to; private ranges count too."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip.split("/")[0].strip())
    except ValueError:
        return None
    bits = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def parallel_sign_in_days(
    sign_ins: Iterable[tuple[datetime, str | None]], session_length: timedelta
) -> int:
    """Distinct days with two sign-ins from different networks within one session length."""
    ordered = sorted((at, net) for at, ip in sign_ins if (net := network(ip)) is not None)
    days: set[date] = set()
    for index, (at, net) in enumerate(ordered):
        for later, other in ordered[index + 1 :]:
            if later - at >= session_length:
                break
            if other != net:
                days.add(later.date())
                break
    return len(days)


def peak_per_hour(times: Iterable[datetime]) -> int:
    """The most events inside any 60-minute window."""
    ordered = sorted(times)
    peak = start = 0
    for end, at in enumerate(ordered):
        while at - ordered[start] >= timedelta(hours=1):
            start += 1
        peak = max(peak, end - start + 1)
    return peak


def distinct_days(times: Iterable[datetime]) -> int:
    return len({at.date() for at in times})


async def _events(
    session: AsyncSession, action: str, since: datetime
) -> dict[uuid.UUID, list[tuple[datetime, str | None]]]:
    rows = await session.execute(
        select(AuditEvent.actor_id, AuditEvent.created_at, AuditEvent.ip).where(
            AuditEvent.action == action, AuditEvent.created_at >= since
        )
    )
    events: dict[uuid.UUID, list[tuple[datetime, str | None]]] = defaultdict(list)
    for actor_id, created_at, ip in rows:
        if actor_id is not None:
            events[actor_id].append((_aware(created_at), str(ip) if ip is not None else None))
    return events


async def _live_locks(session: AsyncSession, now: datetime) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(Task.locked_by_id, func.count())
        .where(
            Task.status == TaskStatus.IN_PROGRESS,
            Task.locked_by_id.is_not(None),
            Task.locked_until > now,
        )
        .group_by(Task.locked_by_id)
    )
    return {user_id: int(count) for user_id, count in rows if user_id is not None}


def _candidates(
    sign_ins: _Events,
    submits: _Events,
    locks: Mapping[uuid.UUID, int],
    session_length: timedelta,
) -> list[tuple[NoticeKind, uuid.UUID, int]]:
    found: list[tuple[NoticeKind, uuid.UUID, int]] = []
    for user_id, events in sign_ins.items():
        days = parallel_sign_in_days(events, session_length)
        if days >= PARALLEL_SIGN_IN_DAYS:
            found.append((NoticeKind.PARALLEL_SIGN_INS, user_id, days))
    for user_id, held in locks.items():
        if held >= PARALLEL_TASK_LOCKS:
            found.append((NoticeKind.PARALLEL_TASKS, user_id, held))
    for user_id, events in submits.items():
        times = [at for at, _ip in events]
        if (peak := peak_per_hour(times)) >= SUPERHUMAN_PER_HOUR:
            found.append((NoticeKind.SUPERHUMAN_PACE, user_id, peak))
        if (days := distinct_days(times)) >= SERVICE_ANNOTATION_DAYS:
            found.append((NoticeKind.SERVICE_ACCOUNT_ANNOTATING, user_id, days))
    return found


def _describe(kind: NoticeKind, count: int, session_length: timedelta) -> str:
    minutes = int(session_length.total_seconds() // 60)
    match kind:
        case NoticeKind.PARALLEL_SIGN_INS:
            return (
                f"Signed in from two different networks less than {minutes} minutes apart "
                f"on {count} days. One account may be shared by several people."
            )
        case NoticeKind.PARALLEL_TASKS:
            return f"Holds {count} task locks at once. Several people may be using one account."
        case NoticeKind.SUPERHUMAN_PACE:
            return (
                f"Submitted {count} annotations within one hour — faster than one person "
                "annotates. The account may be shared or scripted."
            )
        case NoticeKind.SERVICE_ACCOUNT_ANNOTATING:
            return (
                f"A service account submitted annotations on {count} days. People "
                "annotating through a service account still count as seats."
            )


async def usage_notices(
    session: AsyncSession, *, session_length: timedelta, now: datetime | None = None
) -> list[UsageNotice]:
    """Every LIC-31 notice for the last 30 days, sorted by kind, then email."""
    moment = now or datetime.now(UTC)
    since = moment - WINDOW
    sign_ins = await _events(session, "auth.login", since)
    submits = await _events(session, "annotation.submit", since)
    locks = await _live_locks(session, moment)
    candidates = _candidates(sign_ins, submits, locks, session_length)
    user_ids = {user_id for _kind, user_id, _count in candidates}
    users = (
        {user.id: user for user in await session.scalars(select(User).where(User.id.in_(user_ids)))}
        if user_ids
        else {}
    )

    notices: list[UsageNotice] = []
    for kind, user_id, count in candidates:
        user = users.get(user_id)
        # Service accounts only for their own kind; every other kind is about people.
        if user is None or user.is_service != (kind is NoticeKind.SERVICE_ACCOUNT_ANNOTATING):
            continue
        notices.append(
            UsageNotice(
                kind=kind,
                user_id=user_id,
                email=user.email,
                display_name=user.display_name,
                detail=_describe(kind, count, session_length),
                count=count,
            )
        )
    order = list(NoticeKind)
    notices.sort(key=lambda notice: (order.index(notice.kind), notice.email))
    return notices
