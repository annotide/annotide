"""Bulk operations on a project's items (WF-8): assign, return, approve, tag.

Every action walks the requested items one by one and applies the same
rules the single-item endpoints do — `open_task` / `apply_task_update` for
assignment, `return_to_queue` for release, `apply_verdict` for approval —
so a bulk request cannot do anything a hundred single clicks could not.
Items the action does not fit are reported in `BulkResult.skipped` with a
reason instead of failing the request; every per-item check runs before
that item is mutated, so a skip leaves nothing half-applied. The caller
commits once at the end.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.models import AnnotationStatus, Item, ItemStatus, Task, TaskStatus, TaskType
from app.schemas import (
    BulkApprove,
    BulkAssign,
    BulkReject,
    BulkRequest,
    BulkResult,
    BulkReturn,
    BulkSkipped,
    BulkTag,
)
from app.services.annotations import latest_version
from app.services.repository import path_scope_clause
from app.services.reviewing import apply_verdict
from app.services.tasks import (
    apply_task_update,
    ensure_assignee_member,
    open_annotate_tasks,
    open_task,
    return_to_queue,
)
from app.services.workflow import WorkflowConfig, WorkflowError

#: Item statuses in which an `annotate` / `review` task may be opened by hand.
_OPENABLE: dict[TaskType, frozenset[ItemStatus]] = {
    TaskType.ANNOTATE: frozenset(
        {ItemStatus.NEW, ItemStatus.PRELABELED, ItemStatus.ANNOTATING, ItemStatus.REJECTED}
    ),
    TaskType.REVIEW: frozenset({ItemStatus.SUBMITTED, ItemStatus.IN_REVIEW}),
}

#: Item statuses that hold a version awaiting a verdict.
_REVIEWABLE = frozenset({ItemStatus.SUBMITTED, ItemStatus.IN_REVIEW})

TAGS_KEY = "tags"


class _Outcome:
    """Mutable tally for one bulk request."""

    def __init__(self) -> None:
        self.applied = 0
        self.skipped: list[BulkSkipped] = []

    def skip(self, item_id: UUID, reason: str) -> None:
        self.skipped.append(BulkSkipped(item_id=item_id, reason=reason))

    def result(self) -> BulkResult:
        return BulkResult(applied=self.applied, skipped=self.skipped)


async def _load_items(
    session: AsyncSession,
    project_id: UUID,
    item_ids: Iterable[UUID],
    outcome: _Outcome,
    path_prefixes: list[str] | None = None,
) -> list[Item]:
    """The requested items that belong to `project_id`, in request order;
    ids from another project, nowhere, or outside `path_prefixes` are skipped
    as not found."""
    wanted = list(dict.fromkeys(item_ids))  # de-duplicate, keep order
    stmt = select(Item).where(Item.project_id == project_id, Item.id.in_(wanted))
    if path_prefixes:
        stmt = stmt.where(path_scope_clause(Item.path, path_prefixes))
    rows = await session.scalars(stmt)
    by_id = {item.id: item for item in rows}
    for item_id in wanted:
        if item_id not in by_id:
            outcome.skip(item_id, "not found in this project")
    return [by_id[item_id] for item_id in wanted if item_id in by_id]


async def _live_tasks(session: AsyncSession, items: Sequence[Item]) -> dict[UUID, list[Task]]:
    """Open / in-progress tasks per item id."""
    if not items:
        return {}
    rows = await session.scalars(
        select(Task).where(
            Task.item_id.in_([item.id for item in items]),
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
    )
    tasks: dict[UUID, list[Task]] = {}
    for task in rows:
        tasks.setdefault(task.item_id, []).append(task)
    return tasks


async def bulk_items(
    session: AsyncSession,
    *,
    project_id: UUID,
    request: BulkRequest,
    actor_id: UUID,
    organization_id: UUID,
    role: str,
    config: WorkflowConfig,
    ip: str | None = None,
    path_prefixes: list[str] | None = None,
) -> BulkResult:
    """Apply `request` to its items. Does not commit.

    With `path_prefixes` (a folder-limited caller), items outside them are
    skipped as not found, exactly like ids from another project.
    """
    outcome = _Outcome()
    items = await _load_items(session, project_id, request.item_ids, outcome, path_prefixes)
    match request:
        case BulkAssign():
            await _assign(session, items, request, outcome, config=config)
        case BulkReturn():
            await _return(session, items, outcome)
        case BulkApprove() | BulkReject():
            await _verdict(
                session,
                items,
                request,
                outcome,
                actor_id=actor_id,
                organization_id=organization_id,
                role=role,
                config=config,
                ip=ip,
            )
        case BulkTag():
            _tag(items, request, outcome)
    return outcome.result()


async def _assign(
    session: AsyncSession,
    items: Sequence[Item],
    request: BulkAssign,
    outcome: _Outcome,
    *,
    config: WorkflowConfig,
) -> None:
    task_type = TaskType(request.type.value)
    live = await _live_tasks(session, items)
    # Only the keys the client sent change (see `TaskUpdate`); the rest of
    # the request is the discriminator and the item list.
    given = request.model_fields_set & {"assignee_id", "priority", "deadline"}
    updates = {name: getattr(request, name) for name in given}
    if items:
        await ensure_assignee_member(session, items[0].project_id, updates.get("assignee_id"))
    for item in items:
        tasks = [task for task in live.get(item.id, []) if task.type == task_type]
        if tasks:
            task = tasks[0]
            if task.status is TaskStatus.IN_PROGRESS and "assignee_id" in updates:
                outcome.skip(item.id, f"{task_type.value} task is in progress; return it first")
                continue
            apply_task_update(task, **updates)
            outcome.applied += 1
            continue
        if item.status not in _OPENABLE[task_type]:
            outcome.skip(item.id, f"status {item.status.value!r} has no {task_type.value} work")
            continue
        if task_type is TaskType.ANNOTATE:
            # QA-1: fans out to N consensus slots when the project asks for
            # them; a no-op when the item already carries a live annotate
            # task of any kind (ordinary, consensus, region, gold).
            await open_annotate_tasks(
                session,
                item_id=item.id,
                project_id=item.project_id,
                config=config,
                assignee_id=updates.get("assignee_id"),
                priority=updates.get("priority") or 0,
                deadline=updates.get("deadline"),
            )
        else:
            await open_task(
                session,
                item_id=item.id,
                project_id=item.project_id,
                task_type=task_type,
                assignee_id=updates.get("assignee_id"),
                priority=updates.get("priority") or 0,
                deadline=updates.get("deadline"),
            )
        outcome.applied += 1


async def _return(session: AsyncSession, items: Sequence[Item], outcome: _Outcome) -> None:
    live = await _live_tasks(session, items)
    for item in items:
        held = [t for t in live.get(item.id, []) if t.status is TaskStatus.IN_PROGRESS]
        if not held:
            outcome.skip(item.id, "no task in progress")
            continue
        for task in held:
            return_to_queue(task)
        outcome.applied += 1


async def _verdict(
    session: AsyncSession,
    items: Sequence[Item],
    request: BulkApprove | BulkReject,
    outcome: _Outcome,
    *,
    actor_id: UUID,
    organization_id: UUID,
    role: str,
    config: WorkflowConfig,
    ip: str | None,
) -> None:
    for item in items:
        if item.status not in _REVIEWABLE:
            outcome.skip(item.id, f"status {item.status.value!r} is not awaiting review")
            continue
        annotation = await latest_version(session, item.id)
        if annotation is None or annotation.status is not AnnotationStatus.SUBMITTED:
            outcome.skip(item.id, f"no submitted version to {request.action}")
            continue
        try:
            await apply_verdict(
                session,
                annotation=annotation,
                item=item,
                role=role,
                config=config,
                reviewer_id=actor_id,
                organization_id=organization_id,
                approve=isinstance(request, BulkApprove),
                comment=request.comment,
                ip=ip,
            )
        except (WorkflowError, ApiError) as exc:
            # Self-review forbidden, role may not review, ...: the checks
            # run before any write, so skipping here leaves the item intact.
            outcome.skip(item.id, str(exc))
            continue
        outcome.applied += 1


def _tag(items: Sequence[Item], request: BulkTag, outcome: _Outcome) -> None:
    add = set(request.add)
    remove = set(request.remove)
    for item in items:
        current = item.meta.get(TAGS_KEY, [])
        existing = set(current) if isinstance(current, list) else set()
        tags = sorted((existing | add) - remove)
        if tags == sorted(existing):
            outcome.skip(item.id, "tags unchanged")
            continue
        item.meta = {**item.meta, TAGS_KEY: tags}
        outcome.applied += 1


__all__ = ["TAGS_KEY", "bulk_items"]
