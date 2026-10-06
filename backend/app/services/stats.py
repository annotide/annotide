"""Project dashboard numbers (UX-5): progress, throughput, rejection rate, class balance.

Everything is computed on request from the live tables; there is no
materialised view yet. The three heavier passes — class balance, the
per-annotator table and the throughput series — pull rows into Python instead
of aggregating in SQL, because the class counts live inside the ``result``
JSONB and the tests run on SQLite, which has no JSONB operators. At MVP
scale (thousands of items) this is a few milliseconds; revisit before tiling.

Known wrinkle, shared with the review endpoint: a review changes the
annotation's ``status`` in place rather than adding a version, so a version
that was submitted on Monday and approved on Tuesday counts as *approved* on
Monday in ``throughput``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    ItemStatus,
    Task,
    TaskType,
    User,
)
from app.schemas import (
    AnnotationStats,
    AnnotatorStats,
    ClassCount,
    ItemStats,
    ProjectStats,
    ReviewStats,
    TaskStats,
    TaskTypeStats,
    ThroughputDay,
)
from app.services.repository import path_scope_clause

DEFAULT_DAYS = 14
MAX_DAYS = 90
MAX_CLASSES = 50

_REVIEWED = (AnnotationStatus.APPROVED, AnnotationStatus.REJECTED)
_COUNTED = (AnnotationStatus.SUBMITTED, *_REVIEWED)


def _item_scope(project_id: UUID, path_prefixes: list[str] | None) -> list[Any]:
    """WHERE terms on `Item` for the project, narrowed to a member's folders."""
    terms: list[Any] = [Item.project_id == project_id]
    if path_prefixes:
        terms.append(path_scope_clause(Item.path, path_prefixes))
    return terms


async def project_stats(
    session: AsyncSession,
    project_id: UUID,
    *,
    days: int = DEFAULT_DAYS,
    path_prefixes: list[str] | None = None,
) -> ProjectStats:
    """Assemble every dashboard panel for one project in a single call.

    `path_prefixes` (a folder-limited member) counts only items under them,
    so the dashboard never reveals the size or content of other folders.
    """
    scope = _item_scope(project_id, path_prefixes)
    items = await _item_stats(session, scope)
    tasks = await _task_stats(session, scope)
    versions, by_source = await _version_counts(session, scope)
    latest = await _latest_versions(session, scope)

    latest_by_status: Counter[str] = Counter(status.value for _, status, _, _ in latest)
    approved = latest_by_status[AnnotationStatus.APPROVED.value]
    rejected = latest_by_status[AnnotationStatus.REJECTED.value]
    reviewed = approved + rejected

    return ProjectStats(
        items=items,
        tasks=tasks,
        annotations=AnnotationStats(
            versions=versions,
            by_source=by_source,
            latest_by_status={s.value: latest_by_status[s.value] for s in AnnotationStatus},
        ),
        review=ReviewStats(
            approved=approved,
            rejected=rejected,
            rejection_rate=rejected / reviewed if reviewed else 0.0,
        ),
        throughput=await _throughput(session, scope, days=days),
        classes=_class_balance(latest),
        annotators=await _annotators(session, latest),
    )


async def _item_stats(session: AsyncSession, scope: list[Any]) -> ItemStats:
    rows = await session.execute(
        select(Item.status, func.count()).where(*scope).group_by(Item.status)
    )
    counts = {status.value: n for status, n in rows.all()}
    return ItemStats(
        total=sum(counts.values()),
        by_status={s.value: counts.get(s.value, 0) for s in ItemStatus},
    )


async def _task_stats(session: AsyncSession, scope: list[Any]) -> TaskStats:
    rows = await session.execute(
        select(Task.type, Task.status, func.count())
        .join(Item, Item.id == Task.item_id)
        .where(*scope)
        .group_by(Task.type, Task.status)
    )
    per_type: dict[TaskType, dict[str, int]] = defaultdict(dict)
    for task_type, status, n in rows.all():
        per_type[task_type][status.value] = n
    return TaskStats(
        annotate=TaskTypeStats(**per_type[TaskType.ANNOTATE]),
        review=TaskTypeStats(**per_type[TaskType.REVIEW]),
    )


async def _version_counts(session: AsyncSession, scope: list[Any]) -> tuple[int, dict[str, int]]:
    rows = await session.execute(
        select(Annotation.source, func.count())
        .join(Item, Item.id == Annotation.item_id)
        .where(*scope)
        .group_by(Annotation.source)
    )
    by_source = {source.value: n for source, n in rows.all()}
    return sum(by_source.values()), by_source


async def _latest_versions(
    session: AsyncSession, scope: list[Any]
) -> list[tuple[UUID, AnnotationStatus, UUID | None, dict[str, Any]]]:
    """(item_id, status, author_user_id, result) of the newest version of every item."""
    newest = (
        select(Annotation.item_id, func.max(Annotation.version).label("version"))
        .join(Item, Item.id == Annotation.item_id)
        .where(*scope, Annotation.kind == AnnotationKind.PRIMARY)
        .group_by(Annotation.item_id)
        .subquery()
    )
    rows = await session.execute(
        select(
            Annotation.item_id, Annotation.status, Annotation.author_user_id, Annotation.result
        ).join(
            newest,
            (Annotation.item_id == newest.c.item_id) & (Annotation.version == newest.c.version),
        )
    )
    return [(item_id, status, author, result) for item_id, status, author, result in rows.all()]


async def _throughput(session: AsyncSession, scope: list[Any], *, days: int) -> list[ThroughputDay]:
    """One row per calendar day (UTC) for the last ``days`` days, oldest first, gaps zero-filled."""
    today = datetime.now(UTC).date()
    first = today - timedelta(days=days - 1)
    cutoff = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
    rows = await session.execute(
        select(Annotation.created_at, Annotation.status)
        .join(Item, Item.id == Annotation.item_id)
        .where(
            *scope,
            Annotation.status.in_(_COUNTED),
            Annotation.created_at >= cutoff,
        )
    )
    per_day: dict[Any, Counter[str]] = defaultdict(Counter)
    for created_at, status in rows.all():
        # SQLite hands back naive datetimes; treat them as UTC like the rest of the app.
        stamp = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        per_day[stamp.astimezone(UTC).date()][status.value] += 1
    return [
        ThroughputDay(day=first + timedelta(days=offset), **per_day[first + timedelta(days=offset)])
        for offset in range(days)
    ]


def _class_balance(
    latest: list[tuple[UUID, AnnotationStatus, UUID | None, dict[str, Any]]],
) -> list[ClassCount]:
    """Label counts over the latest non-draft version of every item, most frequent first.

    Shapes count by their ``class``; classifications count as ``"{key}: {value}"``
    so a classification-only project still gets a balance chart.
    """
    counts: Counter[str] = Counter()
    for _, status, _, result in latest:
        if status is AnnotationStatus.DRAFT:
            continue
        for shape in result.get("shapes") or []:
            label = shape.get("class") if isinstance(shape, dict) else None
            if isinstance(label, str):
                counts[label] += 1
        classification = result.get("classification") or {}
        if isinstance(classification, dict):
            for key, value in classification.items():
                counts[f"{key}: {value}"] += 1
    return [
        ClassCount(label=label, count=n)
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CLASSES]
    ]


async def _annotators(
    session: AsyncSession,
    latest: list[tuple[UUID, AnnotationStatus, UUID | None, dict[str, Any]]],
) -> list[AnnotatorStats]:
    """Per-author submitted / approved / rejected counts over latest versions, busiest first."""
    per_user: dict[UUID, Counter[str]] = defaultdict(Counter)
    for _, status, author, _ in latest:
        if author is not None and status in _COUNTED:
            per_user[author][status.value] += 1
    if not per_user:
        return []
    rows = await session.execute(
        select(User.id, User.display_name).where(User.id.in_(per_user.keys()))
    )
    names: dict[UUID, str] = dict(rows.tuples().all())
    stats = [
        AnnotatorStats(user_id=user_id, display_name=names.get(user_id, "unknown"), **counts)
        for user_id, counts in per_user.items()
    ]
    stats.sort(key=lambda s: (-(s.submitted + s.approved + s.rejected), s.display_name))
    return stats
