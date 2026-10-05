"""Item lifecycle steps that touch both the status machine and the task queue (WF-1).

Submitting, reviewing and skipping each change the item's status *and* open
or close task rows, and how they do so depends on the project's workflow
config. Keeping the two halves together here means a router cannot apply one
without the other, and the config is read in exactly one place.

Pure status rules stay in `services/workflow.py`; task bookkeeping stays in
`services/tasks.py`. This module sequences them.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ForbiddenError
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    ItemStatus,
    Project,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas.project import RejectionTarget, ReviewMode, WorkflowConfig
from app.services.tasks import complete_task, complete_task_row, open_task
from app.services.webhooks import emit_event
from app.services.workflow import Trigger, next_status

__all__ = ["load_workflow", "review_item", "skip_item", "submit_item"]


async def load_workflow(session: AsyncSession, project_id: UUID) -> WorkflowConfig:
    """The project's workflow config; `{}` and legacy rows give the default flow."""
    project = await session.get(Project, project_id)
    raw = project.workflow if project is not None else {}
    return WorkflowConfig.model_validate(raw)


async def submit_item(
    session: AsyncSession,
    *,
    item: Item,
    role: str,
    config: WorkflowConfig,
    annotation: Annotation | None = None,
    task: Task | None = None,
) -> None:
    """Move a submitted item on: to review, or straight to approved (WF-1).

    A first version on an untouched item starts the work first, so a one-click
    submit from `new` / `prelabeled` / `rejected` is legal. With `annotation`
    (the version just submitted) an `annotation.submitted` webhook event is
    queued in the same transaction (API-4).

    `task` is the specific annotate task this save resolved to (CONTRACTS.md
    *task*, "which task a save belongs to"), when any: an item can carry
    several live annotate tasks at once (consensus slots, QA-1; regions,
    IMG-6), so a submit must close *that* task, not "any live annotate task"
    (the old, single-task behaviour, used when `task` is `None`). A gold task
    (QA-4) closes and nothing else happens — the item's status, its other
    tasks and its `primary` line are untouched, no review task opens and no
    webhook fires. Otherwise the item moves on to `submitted` (and opens a
    review task) only once no live annotate task remains on it — the last of
    N consensus slots, or the last region.
    """
    if task is not None and task.gold:
        complete_task_row(task)
        return

    if item.status in (ItemStatus.NEW, ItemStatus.PRELABELED, ItemStatus.REJECTED):
        item.status = next_status(item.status, Trigger.ASSIGN, role=role, config=config)

    if task is not None:
        complete_task_row(task)
        closed: Task | None = task
    else:
        closed = await complete_task(session, item_id=item.id, task_type=TaskType.ANNOTATE)

    remaining = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.item_id == item.id,
            Task.type == TaskType.ANNOTATE,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
    )
    if remaining:
        # Another slot / region is still live: the item stays `annotating`
        # until the last one closes.
        if annotation is not None:
            await emit_annotation_event(
                session, item=item, annotation=annotation, event="submitted"
            )
        return

    config = await effective_review_config(session, item=item, config=config)
    item.status = next_status(item.status, Trigger.SUBMIT, role=role, config=config)
    if config.review is ReviewMode.REQUIRED:
        # The review inherits the last-closed annotate task's urgency (WF-6).
        await open_task(
            session,
            item_id=item.id,
            project_id=item.project_id,
            task_type=TaskType.REVIEW,
            priority=closed.priority if closed is not None else 0,
            deadline=closed.deadline if closed is not None else None,
        )
    if annotation is not None:
        await emit_annotation_event(session, item=item, annotation=annotation, event="submitted")


def in_review_sample(item_id: UUID, rate: float) -> bool:
    """Whether `review: sampled` sends this item to review (QA-7).

    Deterministic — the first 8 bytes of sha256(str(item_id)) as a fraction
    of 2**64 compared with `rate` — so a retried submit decides the same way
    and the sample can be recomputed later (CONTRACTS.md *Project workflow*).
    """
    digest = hashlib.sha256(str(item_id).encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


async def effective_review_config(
    session: AsyncSession, *, item: Item, config: WorkflowConfig
) -> WorkflowConfig:
    """Resolve `review: sampled` to `required` or `none` for this item (QA-7).

    In the sample, or ever rejected before (a rejected item is always
    reviewed again): `required`. Otherwise `none` — the submit approves.
    """
    if config.review is not ReviewMode.SAMPLED:
        return config
    reviewed = in_review_sample(item.id, config.review_sample_rate)
    if not reviewed:
        rejected_before = await session.scalar(
            select(Annotation.id)
            .where(
                Annotation.item_id == item.id,
                Annotation.kind == AnnotationKind.PRIMARY,
                Annotation.status == AnnotationStatus.REJECTED,
            )
            .limit(1)
        )
        reviewed = rejected_before is not None
    mode = ReviewMode.REQUIRED if reviewed else ReviewMode.NONE
    return config.model_copy(update={"review": mode})


async def emit_annotation_event(
    session: AsyncSession, *, item: Item, annotation: Annotation, event: str, **extra: object
) -> None:
    """Queue `annotation.<event>` for the project's webhook subscribers (API-4).

    When the item has just reached `approved` — a verdict, or a submit that
    needs no review (`none`, or `sampled` outside the sample) — `item.approved`
    follows, so a pipeline has one event for "this item is done" whichever
    path the project takes.
    """
    project = await session.get(Project, item.project_id)
    if project is None:
        return
    await emit_event(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        event=f"annotation.{event}",
        payload={
            "annotation_id": str(annotation.id),
            "item_id": str(item.id),
            "item_path": item.path,
            "item_status": item.status.value,
            "version": annotation.version,
            "author_user_id": str(annotation.author_user_id) if annotation.author_user_id else None,
            **extra,
        },
    )
    if item.status is ItemStatus.APPROVED and event in ("submitted", "approved"):
        await emit_event(
            session,
            organization_id=project.organization_id,
            project_id=project.id,
            event="item.approved",
            payload={
                "item_id": str(item.id),
                "item_path": item.path,
                "annotation_id": str(annotation.id),
                "version": annotation.version,
                "via": "review" if event == "approved" else "no_review",
            },
        )


async def review_item(
    session: AsyncSession,
    *,
    item: Item,
    role: str,
    config: WorkflowConfig,
    approve: bool,
    author_id: UUID | None,
    reviewer_id: UUID,
) -> None:
    """Apply a verdict: close the review task and, on rejection, reopen annotation.

    `author_id` is who wrote the reviewed version; `rejection_returns_to`
    decides whether they get the item back or the queue does. Self-review is
    refused when the project forbids it, superusers included: the rule is
    about the project's process, not about privilege.
    """
    if not config.allow_self_review and author_id is not None and author_id == reviewer_id:
        raise ForbiddenError("This project does not allow reviewing your own annotation.")
    trigger = Trigger.APPROVE if approve else Trigger.REJECT
    item.status = next_status(item.status, trigger, role=role, config=config)
    closed = await complete_task(session, item_id=item.id, task_type=TaskType.REVIEW)
    if not approve:
        assignee = (
            author_id if config.rejection_returns_to is RejectionTarget.SAME_ANNOTATOR else None
        )
        # A rejected item keeps its place in the queue (WF-6).
        await open_task(
            session,
            item_id=item.id,
            project_id=item.project_id,
            task_type=TaskType.ANNOTATE,
            assignee_id=assignee,
            priority=closed.priority if closed is not None else 0,
            deadline=closed.deadline if closed is not None else None,
        )


async def skip_item(
    session: AsyncSession, *, item: Item, role: str, config: WorkflowConfig, reason: str
) -> None:
    """Skip an item, recording why (TOOL-6); refused when the project disallows it."""
    item.status = next_status(item.status, Trigger.SKIP, role=role, config=config)
    item.meta = {**item.meta, "skip_reason": reason}
    # A skipped item leaves the queue: its annotate task is done (WF-2).
    await complete_task(session, item_id=item.id, task_type=TaskType.ANNOTATE)
