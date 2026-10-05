"""Task-claiming and locking logic (WF-2, WF-3).

Kept out of `api/v1/tasks.py` because the claim query is the subtlest part of
the system: it has to hand out at most one task per claim, even when many
annotators hit "next task" at the same instant, with no application-level
mutex.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, case, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.errors import ConflictError, TaskLockedError, ValidationFailedError
from app.models import (
    Annotation,
    AnnotationKind,
    Item,
    Membership,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas.project import WorkflowConfig
from app.services.repository import path_scope_clause


async def ensure_assignee_member(
    session: AsyncSession, project_id: UUID, assignee_id: UUID | None
) -> None:
    """Refuse an assignee who is not a member of `project_id` (`ValidationFailedError`).

    Handing a task to a user outside the project would pin work to someone who
    can never claim it and would name a foreign user id in this project's
    data. `None` (return to the shared queue) is always fine.
    """
    if assignee_id is None:
        return
    found = await session.scalar(
        select(Membership.user_id).where(
            Membership.project_id == project_id, Membership.user_id == assignee_id
        )
    )
    if found is None:
        raise ValidationFailedError(f"User {assignee_id} is not a member of this project.")


async def open_task(
    session: AsyncSession,
    *,
    item_id: UUID,
    project_id: UUID,
    task_type: TaskType,
    assignee_id: UUID | None = None,
    priority: int = 0,
    deadline: datetime | None = None,
    slot: int | None = None,
    region: list[float] | None = None,
    gold: bool = False,
) -> Task | None:
    """Open a new task on `item_id`, unless one of `task_type` is already live (WF-2, WF-4).

    Liveness is keyed on `(item, type, slot, region, gold)`: an item may carry
    several live annotate tasks at once — one per consensus slot (QA-1), per
    region (IMG-6), or a gold task per assignee (QA-4) — but never two for the
    same key. Gold tasks are additionally keyed on `assignee_id`.

    `priority` and `deadline` order the queue (WF-6): higher priority first,
    then the earliest deadline, then the oldest task. Callers that reopen
    work — a rejection returning an item to annotation — pass the closed
    task's values on so the item keeps its place in the queue.

    Idempotent: if the item already has an `open` or `in_progress` task of
    this type, this does nothing and returns `None` — a re-scan of an item
    with an outstanding annotate task must not pile up a second one, and a
    submit that races a resubmission must not open two review tasks. Callers
    own the transaction; this only `session.add`s and never commits, so
    scanning can batch many of these into one commit and the annotation
    router can commit once at the end of its own transition.
    """
    live = await session.scalars(
        select(Task).where(
            Task.item_id == item_id,
            Task.type == task_type,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
            Task.slot.is_(None) if slot is None else Task.slot == slot,
            Task.gold == gold,
        )
    )
    # `region` is JSONB; compared in Python so SQLite and Postgres agree.
    for existing in live:
        if existing.region == region and (not gold or existing.assignee_id == assignee_id):
            return None

    task = Task(
        item_id=item_id,
        project_id=project_id,
        type=task_type,
        assignee_id=assignee_id,
        priority=priority,
        deadline=deadline,
        slot=slot,
        region=region,
        gold=gold,
    )
    session.add(task)
    return task


async def open_annotate_tasks(
    session: AsyncSession,
    *,
    item_id: UUID,
    project_id: UUID,
    config: WorkflowConfig,
    assignee_id: UUID | None = None,
    priority: int = 0,
    deadline: datetime | None = None,
) -> list[Task]:
    """Open the *first* annotate task(s) of an item (QA-1, CONTRACTS.md *task*).

    Wherever the system opens an item's first annotate task — a source or
    upload scan, an import, a prelabel run, `POST /projects/{id}/tasks`
    without an explicit `slot`, or bulk `assign` — it goes through here so
    consensus fan-out lives in one place: with `config.consensus_annotators`
    = N > 1 this opens N unassigned slot tasks (`slot` 0..N-1) instead of one
    ordinary task, and nothing when the item already carries *any* live
    annotate task (ordinary, consensus, region or gold) — an item split into
    regions or already mid-consensus must not also grow an ordinary task, and
    changing `consensus_annotators` later does not retroactively fan out an
    item whose first task already opened.

    `assignee_id` only applies to the ordinary (N == 1) task: consensus slots
    are always opened unassigned, for the queue to hand out one per person.
    """
    already_live = await session.scalar(
        select(Task.id)
        .where(
            Task.item_id == item_id,
            Task.type == TaskType.ANNOTATE,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
        .limit(1)
    )
    if already_live is not None:
        return []

    if config.consensus_annotators <= 1:
        task = await open_task(
            session,
            item_id=item_id,
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            assignee_id=assignee_id,
            priority=priority,
            deadline=deadline,
        )
        return [task] if task is not None else []

    opened: list[Task] = []
    for slot in range(config.consensus_annotators):
        task = await open_task(
            session,
            item_id=item_id,
            project_id=project_id,
            task_type=TaskType.ANNOTATE,
            priority=priority,
            deadline=deadline,
            slot=slot,
        )
        if task is not None:
            opened.append(task)
    return opened


class Unset(Enum):
    """Sentinel for `update_task`: "leave this field alone".

    `None` is a valid value for `deadline` and `assignee_id` (clear it), so
    absence needs its own marker; an enum member lets mypy narrow on `is not`.
    """

    TOKEN = 0


_UNSET: Literal[Unset.TOKEN] = Unset.TOKEN


async def update_task(
    session: AsyncSession,
    task: Task,
    *,
    priority: int | Unset = _UNSET,
    deadline: datetime | Unset | None = _UNSET,
    assignee_id: UUID | Unset | None = _UNSET,
) -> Task:
    """Re-prioritise, re-schedule or re-assign a live task (WF-6, WF-8).

    Only `open` and `in_progress` tasks can change: a `done` or `cancelled`
    task is history and editing it would rewrite the audit trail
    (`ConflictError`). Reassigning an `in_progress` task is also refused —
    the lock holder is working on it; release it first. Setting
    `assignee_id` on an `open` task pins it to that user (only they can
    claim it), `None` returns it to the shared queue. Commits.
    """
    apply_task_update(task, priority=priority, deadline=deadline, assignee_id=assignee_id)
    await session.commit()
    await session.refresh(task)
    return task


def apply_task_update(
    task: Task,
    *,
    priority: int | Unset = _UNSET,
    deadline: datetime | Unset | None = _UNSET,
    assignee_id: UUID | Unset | None = _UNSET,
) -> None:
    """The checks and field writes of `update_task`, without the commit — for
    callers that change many tasks in one transaction (WF-8)."""
    if task.status not in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS):
        raise ConflictError(f"Task {task.id} is {task.status.value} and can no longer be changed.")
    if priority is not _UNSET:
        task.priority = priority
    if deadline is not _UNSET:
        task.deadline = deadline
    if assignee_id is not _UNSET:
        if task.status == TaskStatus.IN_PROGRESS and assignee_id != task.assignee_id:
            raise ConflictError(f"Task {task.id} is in progress; release it before reassigning.")
        task.assignee_id = assignee_id


async def complete_task(
    session: AsyncSession,
    *,
    item_id: UUID,
    task_type: TaskType,
) -> Task | None:
    """Mark `item_id`'s open/in-progress task of `task_type` `done` (WF-2, WF-4).

    Returns `None` when there was no such task — a caller that submitted
    without ever having claimed a task (e.g. a draft created and submitted
    straight away) should not be treated as an error. Clears the lock along
    with the status change, since a `done` task holds no lock. Does not
    commit; the caller's transition owns the transaction.
    """
    task = await session.scalar(
        select(Task).where(
            Task.item_id == item_id,
            Task.type == task_type,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
    )
    if task is None:
        return None

    complete_task_row(task)
    return task


def complete_task_row(task: Task) -> None:
    """The write-only half of `complete_task`, for callers that already hold
    the specific task to close — parallel annotate tasks (QA-1, QA-4, IMG-6)
    can have several live at once, so closing "any live task of that type"
    is not precise enough once a submit must close *this* slot / region /
    gold task and no other."""
    task.status = TaskStatus.DONE
    task.locked_by_id = None
    task.locked_until = None


async def claim_next_task(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    lock_ttl_seconds: int,
    task_type: TaskType | None = None,
    path_prefixes: list[str] | None = None,
) -> Task | None:
    """Claim and lock the highest-priority open task in a project (WF-2, WF-3).

    `path_prefixes` (a folder-limited member) restricts both tiers to tasks
    on items under those prefixes.

    The race this exists to close: two annotators press "next task" at the
    same moment. A naive implementation — `SELECT` the top candidate, then a
    separate `UPDATE` to claim it — lets both requests read the same row
    before either one writes, so both annotators would be handed the same
    item (a classic read-then-write lost-update race).

    This query closes that window with a single statement:
    `SELECT ... FOR UPDATE SKIP LOCKED` (`Select.with_for_update(skip_locked=
    True)`). `FOR UPDATE` takes a row lock on the candidate row as part of the
    `SELECT` itself, inside the caller's transaction, so no other transaction
    can select *and* lock that same row until this one commits or rolls back.
    `SKIP LOCKED` is what makes this safe for concurrency rather than merely
    correct: without it, a second concurrent claim running the identical query
    would block waiting for the first transaction's lock (serialising every
    claim through one queue, and risking deadlocks against other locking
    statements); with it, the second claim simply skips the row that is
    already locked and locks the next-best candidate instead. Two annotators
    claiming at once therefore always walk away with two different tasks (or
    one of them gets `None`, once the queue truly is empty) — there is no
    window in which both can see and take the same row.

    Eligible tasks, in two tiers:

    1. The caller's own `in_progress` task — `locked_by_id == user_id`,
       whether or not the lock has expired. A full navigation (reload, typed
       URL, closed tab) skips the client's release-on-unmount, so without this
       tier the annotator who refreshes sees "Queue is empty" for up to the
       lock TTL while their own task sits locked. Claiming it again renews the
       lock and hands the same task back.
    2. `status == open`, and either unassigned or assigned to `user_id`, and
       either never locked or with an expired lock (`locked_until` in the
       past).

    Tier 1 always wins over tier 2, so a reload resumes the task in hand
    instead of starting a second one. `task_type`, when given, restricts both
    tiers to that kind of work — a reviewer asking for `review` never gets
    handed an `annotate` task. Within a tier the order is `priority DESC,
    deadline ASC NULLS LAST, created_at ASC`, so the most urgent, then the
    most overdue, then the oldest-waiting task is claimed first.

    On a hit, this stamps the lock (`locked_by_id`, `locked_until = now() +
    lock_ttl_seconds`), moves the task to `in_progress`, and sets `assignee_id
    = user_id` — claiming assigns the task to whoever claims it. Returns
    `None` when no eligible task exists; that is a normal empty-queue state,
    not an error.
    """
    now = datetime.now(UTC)
    own_in_progress = and_(
        Task.status == TaskStatus.IN_PROGRESS,
        Task.locked_by_id == user_id,
    )
    open_and_free = and_(
        Task.status == TaskStatus.OPEN,
        or_(Task.assignee_id.is_(None), Task.assignee_id == user_id),
        or_(Task.locked_until.is_(None), Task.locked_until < now),
    )

    # QA-1: N distinct people annotate a consensus item, so a candidate slot
    # task (`Task.slot` not null) is excluded when this caller already
    # holds or finished (any status but `cancelled`) *another* slot task on
    # the same item, or authored a `consensus` version there.
    other_slot_task = aliased(Task)
    holds_another_slot = (
        select(other_slot_task.id)
        .where(
            other_slot_task.item_id == Task.item_id,
            other_slot_task.id != Task.id,
            other_slot_task.slot.is_not(None),
            other_slot_task.assignee_id == user_id,
            other_slot_task.status != TaskStatus.CANCELLED,
        )
        .exists()
    )
    authored_consensus = (
        select(Annotation.id)
        .where(
            Annotation.item_id == Task.item_id,
            Annotation.kind == AnnotationKind.CONSENSUS,
            Annotation.author_user_id == user_id,
        )
        .exists()
    )
    not_a_second_slot = or_(Task.slot.is_(None), not_(or_(holds_another_slot, authored_consensus)))

    conditions = [
        Task.project_id == project_id,
        or_(own_in_progress, open_and_free),
        not_a_second_slot,
        *(
            [Task.item_id.in_(select(Item.id).where(path_scope_clause(Item.path, path_prefixes)))]
            if path_prefixes
            else []
        ),
    ]
    if task_type is not None:
        conditions.append(Task.type == task_type)
    stmt = (
        select(Task)
        .where(*conditions)
        .order_by(
            case((Task.status == TaskStatus.IN_PROGRESS, 0), else_=1),
            # Gold tasks first (QA-4): they are always the caller's own.
            case((Task.gold.is_(True), 0), else_=1),
            Task.priority.desc(),
            Task.deadline.asc().nulls_last(),
            Task.created_at.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    result = await session.execute(stmt)
    task = result.scalars().first()
    if task is None:
        return None

    task.locked_by_id = user_id
    task.locked_until = now + timedelta(seconds=lock_ttl_seconds)
    task.status = TaskStatus.IN_PROGRESS
    task.assignee_id = user_id

    await session.commit()
    await session.refresh(task)
    return task


async def release_task(
    session: AsyncSession,
    task: Task,
    user_id: UUID,
    *,
    is_superuser: bool = False,
) -> Task:
    """Release the lock on `task` so it re-enters the open queue.

    Only the current lock holder, or a superuser (`is_superuser=True`), may
    release a lock someone else holds — otherwise any project member could
    knock another annotator off an item they are actively working. Anyone
    else gets `TaskLockedError`.

    Releasing a task nobody currently holds is a no-op success (idempotent),
    so a client retrying a release after a timeout does not get an error.
    When a held lock is released, the task also drops back to `open` and its
    `assignee_id` is cleared — locks expire rather than being held forever
    (an annotator who closes the tab must not block an item permanently), and
    a deliberate release is the same signal: this task is free for anyone.
    """
    if task.locked_by_id is not None and task.locked_by_id != user_id and not is_superuser:
        raise TaskLockedError(f"Task {task.id} is locked by another user.")

    return_to_queue(task)
    await session.commit()
    await session.refresh(task)
    return task


def return_to_queue(task: Task) -> None:
    """Drop `task`'s lock and, if it was held, put the task back to `open`
    with no assignee — the release semantics, minus the permission check and
    the commit, for the bulk "return" action (WF-8) and `release_task`."""
    was_locked = task.locked_by_id is not None
    task.locked_by_id = None
    task.locked_until = None
    if was_locked:
        if task.status == TaskStatus.IN_PROGRESS:
            task.status = TaskStatus.OPEN
        # A gold task belongs to its annotator (QA-4): it goes back to them,
        # never to the open queue.
        if not task.gold:
            task.assignee_id = None


async def extend_lock(
    session: AsyncSession,
    task: Task,
    user_id: UUID,
    lock_ttl_seconds: int,
) -> Task:
    """Push a held lock's expiry forward — a heartbeat for a long annotation.

    Only the current holder may extend. Unlike `release_task`, this is not a
    no-op for a non-holder: letting anyone else extend would let them keep
    someone else's lock alive indefinitely, defeating expiry. Refuses with
    `TaskLockedError` (including when the task is not locked at all).
    """
    if task.locked_by_id != user_id:
        raise TaskLockedError(f"Task {task.id} is not locked by this user.")

    task.locked_until = datetime.now(UTC) + timedelta(seconds=lock_ttl_seconds)
    await session.commit()
    await session.refresh(task)
    return task


async def reap_expired_locks(session: AsyncSession, now: datetime | None = None) -> int:
    """Return every `in_progress` task whose lock has expired to the open queue.

    `claim_next_task` only offers `open` tasks to other users, so a task whose
    holder closed the tab (or lost the network) would otherwise stay
    `in_progress` — locked, assigned, invisible to the queue — until someone
    released it by hand. This is the lock-expiry half of that contract: one
    bulk `UPDATE`, same effect as `release_task` on a held lock (`status →
    open`, lock and `assignee_id` cleared). The holder's own reclaim path
    (`claim_next_task` tier 1) does not depend on this, so a reaper running
    once a minute is plenty.

    Commits. Returns the number of tasks reopened.
    """
    now = now or datetime.now(UTC)
    result = await session.execute(
        update(Task)
        .where(Task.status == TaskStatus.IN_PROGRESS, Task.locked_until < now)
        .values(
            status=TaskStatus.OPEN,
            locked_by_id=None,
            locked_until=None,
            # Gold tasks keep their annotator (QA-4); everything else is freed.
            assignee_id=case((Task.gold.is_(True), Task.assignee_id), else_=None),
        )
        # `fetch` syncs already-loaded Task objects from the rows the UPDATE
        # touched; the default `evaluate` would compare `locked_until` in
        # Python, which SQLite (tests) hands back naive.
        .execution_options(synchronize_session="fetch")
    )
    await session.commit()
    # An UPDATE always comes back as a CursorResult; `Result` is the generic
    # return type of `execute` and has no `rowcount`.
    return int(cast(CursorResult[Any], result).rowcount)


def is_lock_expired(task: Task, now: datetime) -> bool:
    """Pure check: has `task`'s lock passed its `locked_until` instant?

    A task that was never locked (`locked_until is None`) is not "expired" —
    there is nothing to expire — so this returns `False` for it; a caller
    that wants "claimable right now" should check `locked_until is None or
    is_lock_expired(task, now)`, which is exactly the condition
    `claim_next_task` filters on. The boundary is inclusive: a lock expiring
    at exactly `now` counts as expired, matching `claim_next_task`'s `<` (a
    task whose `locked_until` equals `now` is not `< now` for a fresh lock,
    but by the next tick it is, and treating the exact instant as already
    expired is the safer default for a heartbeat check).
    """
    return task.locked_until is not None and task.locked_until <= now
