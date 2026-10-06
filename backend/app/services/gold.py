"""Gold references, gold tasks and annotator accuracy (QA-4).

`item.meta.gold_annotation_id` marks an item's approved `primary` version as
the reference every `gold` attempt on that item is scored against. Loads and
orchestrates; the scoring itself is `services/agreement.py`'s `gold_score`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ValidationFailedError
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    Membership,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.schemas.annotation import AnnotationResult
from app.schemas.quality import AnnotatorQuality
from app.services import agreement, audit
from app.services.repository import path_scope_clause
from app.services.tasks import open_task

__all__ = [
    "clear_gold_reference",
    "open_gold_tasks",
    "project_annotator_quality",
    "set_gold_reference",
]


GOLD_ANNOTATION_ID_KEY = "gold_annotation_id"


async def set_gold_reference(
    session: AsyncSession,
    *,
    item: Item,
    annotation_id: UUID,
    actor_id: UUID,
    organization_id: UUID,
    ip: str | None = None,
) -> Item:
    """`PUT /items/{id}/gold`: mark an approved `primary` version as the reference.

    422 unless `annotation_id` is an `approved` `primary` version of this item.
    """
    annotation = await session.get(Annotation, annotation_id)
    if (
        annotation is None
        or annotation.item_id != item.id
        or annotation.kind is not AnnotationKind.PRIMARY
        or annotation.status is not AnnotationStatus.APPROVED
    ):
        raise ValidationFailedError(
            f"Annotation {annotation_id} is not an approved primary version of item {item.id}."
        )

    item.meta = {**item.meta, GOLD_ANNOTATION_ID_KEY: str(annotation_id)}
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="item.gold_set",
        target_type="item",
        target_id=item.id,
        after={GOLD_ANNOTATION_ID_KEY: str(annotation_id)},
        ip=ip,
    )
    return item


async def clear_gold_reference(
    session: AsyncSession,
    *,
    item: Item,
    actor_id: UUID,
    organization_id: UUID,
    ip: str | None = None,
) -> Item:
    """`DELETE /items/{id}/gold`: clear the reference and cancel live gold tasks."""
    new_meta = dict(item.meta)
    new_meta.pop(GOLD_ANNOTATION_ID_KEY, None)
    item.meta = new_meta

    live_gold_tasks = await session.scalars(
        select(Task).where(
            Task.item_id == item.id,
            Task.gold.is_(True),
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
    )
    for task in live_gold_tasks:
        task.status = TaskStatus.CANCELLED
        task.locked_by_id = None
        task.locked_until = None

    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="item.gold_clear",
        target_type="item",
        target_id=item.id,
        before={GOLD_ANNOTATION_ID_KEY: item.meta.get(GOLD_ANNOTATION_ID_KEY)},
        ip=ip,
    )
    return item


async def open_due_gold_task(
    session: AsyncSession, *, project_id: UUID, user_id: UUID, every: int
) -> Task | None:
    """Open a gold task for `user_id` when one is due (`workflow.gold_every`, QA-4).

    Due when the user has closed at least `every - 1` non-gold annotate tasks
    in the project since their latest gold task (ever, if none) and holds no
    live gold task already. The gold item is one they have no gold task on
    yet, any status; none left means nothing is opened. Does not commit: the
    claim that follows hands the task out (gold tasks sort first).
    """
    gold_tasks = list(
        await session.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.gold.is_(True),
                Task.assignee_id == user_id,
            )
        )
    )
    if any(t.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS) for t in gold_tasks):
        return None
    since = max((t.created_at for t in gold_tasks), default=None)
    done = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project_id,
            Task.type == TaskType.ANNOTATE,
            Task.gold.is_(False),
            Task.assignee_id == user_id,
            Task.status == TaskStatus.DONE,
        )
    )
    if since is not None:
        done = done.where(Task.updated_at > since)
    if (await session.scalar(done) or 0) < every - 1:
        return None

    seen = {t.item_id for t in gold_tasks}
    candidates = await session.scalars(
        select(Item).where(Item.project_id == project_id).order_by(Item.created_at, Item.id)
    )
    for item in candidates:
        if item.id in seen or not item.meta.get(GOLD_ANNOTATION_ID_KEY):
            continue
        return await open_task(
            session,
            item_id=item.id,
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            assignee_id=user_id,
            gold=True,
        )
    return None


async def open_gold_tasks(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_ids: list[UUID] | None,
    item_ids: list[UUID] | None,
    priority: int,
) -> tuple[int, int]:
    """`POST /projects/{id}/gold/tasks` (QA-4).

    Defaults to every member with role `annotator` times every item with a
    gold reference. One task per (item, user) that has no gold task of any
    status yet. Raises when a requested `user_id` is not a project member.
    """
    if user_ids is not None:
        rows = await session.scalars(
            select(Membership.user_id).where(
                Membership.project_id == project_id, Membership.user_id.in_(user_ids)
            )
        )
        found = set(rows)
        missing = set(user_ids) - found
        if missing:
            raise ValidationFailedError(
                f"Users not in project {project_id}: {sorted(str(u) for u in missing)}"
            )
        target_user_ids = list(dict.fromkeys(user_ids))
    else:
        rows = await session.scalars(
            select(Membership.user_id).where(
                Membership.project_id == project_id, Membership.role == ProjectRole.ANNOTATOR
            )
        )
        target_user_ids = list(rows)

    item_stmt = select(Item).where(Item.project_id == project_id)
    if item_ids is not None:
        item_stmt = item_stmt.where(Item.id.in_(item_ids))
    target_items = [
        item for item in await session.scalars(item_stmt) if item.meta.get(GOLD_ANNOTATION_ID_KEY)
    ]

    if not target_items or not target_user_ids:
        return 0, 0

    existing = await session.execute(
        select(Task.item_id, Task.assignee_id).where(
            Task.project_id == project_id,
            Task.gold.is_(True),
            Task.item_id.in_([item.id for item in target_items]),
        )
    )
    existing_pairs = {(item_id, assignee_id) for item_id, assignee_id in existing}

    opened = 0
    skipped = 0
    for item in target_items:
        for user_id in target_user_ids:
            if (item.id, user_id) in existing_pairs:
                skipped += 1
                continue
            task = await open_task(
                session,
                item_id=item.id,
                project_id=project_id,
                task_type=TaskType.ANNOTATE,
                assignee_id=user_id,
                priority=priority,
                gold=True,
            )
            if task is None:
                skipped += 1
            else:
                opened += 1
    return opened, skipped


async def _load_users(session: AsyncSession, user_ids: Iterable[UUID]) -> dict[UUID, User]:
    ids = list(set(user_ids))
    if not ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(ids)))
    return {u.id: u for u in rows}


async def project_annotator_quality(
    session: AsyncSession,
    project_id: UUID,
    iou_threshold: float = 0.5,
    path_prefixes: list[str] | None = None,
) -> list[AnnotatorQuality]:
    """`GET /projects/{id}/quality/annotators` (QA-4).

    Latest submitted `gold` version per (user, item) against the item's gold
    reference; ratios `None` without data; `score` is the mean of the
    non-null ratios; sorted by `score` ascending, nulls last.
    `path_prefixes` (a folder-limited reviewer) uses only gold items under them.
    """
    project = await session.get(Project, project_id)
    if project is None:
        return []

    items = select(Item).where(Item.project_id == project_id)
    if path_prefixes:
        items = items.where(path_scope_clause(Item.path, path_prefixes))
    gold_items = {
        item.id: item.meta[GOLD_ANNOTATION_ID_KEY]
        for item in await session.scalars(items)
        if item.meta.get(GOLD_ANNOTATION_ID_KEY)
    }
    if not gold_items:
        return []

    reference_ids = {UUID(str(v)) for v in gold_items.values()}
    references = {
        a.item_id: a
        for a in await session.scalars(select(Annotation).where(Annotation.id.in_(reference_ids)))
    }

    attempt_rows = await session.scalars(
        select(Annotation)
        .where(
            Annotation.item_id.in_(gold_items.keys()),
            Annotation.kind == AnnotationKind.GOLD,
            Annotation.status == AnnotationStatus.SUBMITTED,
        )
        .order_by(Annotation.author_user_id, Annotation.item_id, Annotation.version.desc())
    )
    latest_attempt: dict[tuple[UUID, UUID], Annotation] = {}
    for row in attempt_rows:
        if row.author_user_id is None:
            continue
        key = (row.author_user_id, row.item_id)
        if key not in latest_attempt:
            latest_attempt[key] = row

    per_user: dict[UUID, list[tuple[Annotation, Annotation]]] = defaultdict(list)
    for (user_id, item_id), attempt in latest_attempt.items():
        reference = references.get(item_id)
        if reference is None:
            continue
        per_user[user_id].append((attempt, reference))

    users = await _load_users(session, per_user.keys())
    results: list[AnnotatorQuality] = []
    for user_id, pairs in per_user.items():
        user = users.get(user_id)
        if user is None:
            continue
        classification_scores: list[float] = []
        shape_f1_scores: list[float] = []
        iou_scores: list[float] = []
        span_f1_scores: list[float] = []
        for attempt, reference in pairs:
            score = agreement.gold_score(
                AnnotationResult.model_validate(attempt.result),
                AnnotationResult.model_validate(reference.result),
                iou_threshold,
            )
            if score.classification_accuracy is not None:
                classification_scores.append(score.classification_accuracy)
            if score.shape_f1 is not None:
                shape_f1_scores.append(score.shape_f1)
            if score.mean_iou is not None:
                iou_scores.append(score.mean_iou)
            if score.span_f1 is not None:
                span_f1_scores.append(score.span_f1)

        def _avg(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        classification_accuracy = _avg(classification_scores)
        shape_f1 = _avg(shape_f1_scores)
        mean_iou = _avg(iou_scores)
        span_f1 = _avg(span_f1_scores)
        ratios = [
            r for r in (classification_accuracy, shape_f1, mean_iou, span_f1) if r is not None
        ]
        score_value = sum(ratios) / len(ratios) if ratios else None

        results.append(
            AnnotatorQuality(
                user_id=user_id,
                email=user.email,
                display_name=user.display_name,
                gold_items=len(pairs),
                classification_accuracy=classification_accuracy,
                shape_f1=shape_f1,
                mean_iou=mean_iou,
                span_f1=span_f1,
                score=score_value,
            )
        )

    results.sort(key=lambda r: (r.score is None, r.score if r.score is not None else 0.0))
    return results
