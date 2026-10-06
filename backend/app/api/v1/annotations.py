"""Annotation versions, submit and review (DATA-1, WF-4, QA-6).

Thin by design: the invariants live in :mod:`app.services.annotations` and the
state machine in :mod:`app.services.workflow`. This module only translates HTTP
into those calls.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UNRESTRICTED_LICENCE, ClientIpDep, CurrentUserDep, SessionDep
from app.api.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    Item,
    Model,
    ModelVersion,
    Project,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas import AnnotationResult
from app.services import audit
from app.services.annotations import (
    create_version,
    diff_shapes,
    latest_version,
    resolve_annotate_task,
    task_kind,
)
from app.services.datasets import resolve_schema_version
from app.services.item_flow import load_workflow, submit_item
from app.services.repository import ensure_item_member, get_or_404
from app.services.reviewing import apply_verdict
from app.services.splitting import merge_region_result
from app.services.workflow import ItemStatus, Trigger, next_status

#: Roles that see every kind of version (CONTRACTS.md *annotation*, "Blind
#: annotation"); `ensure_project_member` already maps a superuser to `owner`.
_SEES_EVERYTHING = frozenset({"owner", "reviewer"})

router = APIRouter(tags=["annotations"])


class AnnotationRead(BaseModel):
    """One annotation version as returned by the API."""

    id: UUID
    item_id: UUID
    task_id: UUID | None
    version: int
    source: str
    status: str
    author_user_id: UUID | None
    author_model_version_id: UUID | None
    label_schema_version_id: UUID
    duration_ms: int | None
    result: dict[str, Any]
    #: `primary` / `consensus` / `gold` (QA-1, QA-4); see CONTRACTS.md *annotation*.
    kind: AnnotationKind = AnnotationKind.PRIMARY
    #: Where the outbox publisher wrote this version (DATA-2); `None` until it has.
    blob_path: str | None = None

    model_config = {"from_attributes": True}


class AnnotationCreate(BaseModel):
    """A new annotation version."""

    result: AnnotationResult
    label_schema_version_id: UUID
    task_id: UUID | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    #: Submit straight away instead of saving a draft. A submit runs the QA-6
    #: schema validation; a draft does not, so work in progress can be saved.
    submit: bool = False


class PrelabelCreate(BaseModel):
    """An external producer's pre-label (API-8): a model-authored draft."""

    model_version_id: UUID
    #: In the project schema's classes; no class mapping is applied.
    result: AnnotationResult
    #: Default: the project's latest schema version.
    label_schema_version_id: UUID | None = None


#: An item a person has moved past is never pre-labelled again.
_PRELABEL_STATUSES = frozenset({ItemStatus.NEW, ItemStatus.PRELABELED, ItemStatus.ANNOTATING})


class ReviewRequest(BaseModel):
    """A reviewer's verdict on a submitted annotation (WF-4)."""

    approve: bool
    comment: str | None = None
    #: A reviewer may correct the annotation themselves rather than bouncing it
    #: back; the correction is stored as a new version authored by them.
    corrected_result: AnnotationResult | None = None


async def _item_and_role(
    session: SessionDep, item_id: UUID, user: CurrentUserDep
) -> tuple[Item, str]:
    """Load an item and the caller's role in its project, or refuse."""
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} does not exist.")
    role = await ensure_item_member(session, item, user)
    return item, role


@router.get(
    "/items/{item_id}/annotations",
    response_model=list[AnnotationRead],
    summary="Version history for an item",
)
async def list_annotations(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> list[Annotation]:
    """Every version, newest first. Versions are never deleted (DATA-1).

    Blind annotation (CONTRACTS.md *annotation*): owners, reviewers and
    superusers see every kind. Anyone else sees `primary` versions plus their
    own `consensus` / `gold` versions — and while they hold a live consensus
    or gold task on the item, only their own versions of that kind (no
    prelabel, no other annotator's work, no gold reference).
    """
    _, role = await _item_and_role(session, item_id, current_user)
    rows = await session.scalars(
        select(Annotation).where(Annotation.item_id == item_id).order_by(Annotation.version.desc())
    )
    annotations = list(rows)
    if role in _SEES_EVERYTHING:
        return annotations

    live_kind = await _live_task_kind(session, item_id=item_id, user_id=current_user.id)
    if live_kind is not None:
        return [
            a for a in annotations if a.kind is live_kind and a.author_user_id == current_user.id
        ]
    return [
        a
        for a in annotations
        if a.kind is AnnotationKind.PRIMARY or a.author_user_id == current_user.id
    ]


async def _live_task_kind(
    session: AsyncSession, *, item_id: UUID, user_id: UUID
) -> AnnotationKind | None:
    """`consensus` / `gold` when the caller holds a live task of that kind on
    the item, else `None` — see `_SEES_EVERYTHING`'s docstring."""
    task = await session.scalar(
        select(Task).where(
            Task.item_id == item_id,
            Task.type == TaskType.ANNOTATE,
            Task.assignee_id == user_id,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
            or_(Task.slot.is_not(None), Task.gold.is_(True)),
        )
    )
    if task is None:
        return None
    return AnnotationKind.GOLD if task.gold else AnnotationKind.CONSENSUS


@router.post(
    "/items/{item_id}/annotations",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=AnnotationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new annotation version",
)
async def create_annotation(
    item_id: UUID,
    payload: AnnotationCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Annotation:
    """Append a version. Existing versions are never modified.

    Saving a draft leaves the item in `annotating`; submitting moves it on
    through the workflow machine (`services/item_flow.py`, WF-1) and runs the
    QA-6 validation first.
    """
    item, role = await _item_and_role(session, item_id, current_user)
    config = await load_workflow(session, item.project_id)

    # Which task this save belongs to (CONTRACTS.md *task*, last paragraph):
    # the given `task_id` (must be the caller's own live annotate task, else
    # 409), else their own in-progress annotate task on the item, else none.
    task = await resolve_annotate_task(
        session, item_id=item_id, user_id=current_user.id, task_id=payload.task_id
    )
    kind = task_kind(task)

    result = payload.result
    if task is not None and task.region is not None:
        # IMG-6: a region task's save is merged server-side into the item's
        # whole-image `primary` result before it becomes a version.
        x_min, y_min, x_max, y_max = task.region
        result = await merge_region_result(
            session,
            item_id=item.id,
            region=(x_min, y_min, x_max, y_max),
            submitted=payload.result,
        )

    target_status = AnnotationStatus.SUBMITTED if payload.submit else AnnotationStatus.DRAFT
    annotation = await create_version(
        session,
        item=item,
        result=result,
        label_schema_version_id=payload.label_schema_version_id,
        author_user_id=current_user.id,
        task_id=task.id if task is not None else None,
        duration_ms=payload.duration_ms,
        status=target_status,
        kind=kind,
    )

    if payload.submit:
        await submit_item(
            session, item=item, role=role, config=config, annotation=annotation, task=task
        )
    elif (task is None or not task.gold) and item.status in (
        ItemStatus.NEW,
        ItemStatus.PRELABELED,
        ItemStatus.REJECTED,
    ):
        # The first draft on an untouched item starts the work. The state
        # machine (app/services/workflow.py) decides; this router never
        # compares or assigns a status by hand. A gold task (QA-4) never
        # touches the item's status.
        item.status = next_status(item.status, Trigger.ASSIGN, role=role, config=config)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="annotation.create",
        target_type="annotation",
        target_id=annotation.id,
        after={
            "status": annotation.status.value,
            "version": annotation.version,
            "submit": payload.submit,
        },
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(annotation)
    return annotation


@router.post(
    "/items/{item_id}/prelabels",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=AnnotationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Post a pre-label from an external producer (API-8)",
)
async def create_prelabel(
    item_id: UUID,
    payload: PrelabelCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Annotation:
    """A `draft`, `source: model` version, as the `prelabel` job writes one.

    For agents and offline pipelines that run the model themselves (the MCP
    server's `create_prelabel`). Same rules as the job: never on an item with
    human work (ML-10), and a `new` item becomes `prelabeled`.
    """
    item, role = await _item_and_role(session, item_id, current_user)
    if role not in {"owner", "annotator"}:
        raise ForbiddenError("Only a project owner or annotator may post pre-labels.")
    version = await get_or_404(session, ModelVersion, payload.model_version_id)
    await get_or_404(session, Model, version.model_id, organization_id=current_user.organization_id)

    if item.status not in _PRELABEL_STATUSES:
        raise ConflictError(f"The item is {item.status.value}; it can no longer be pre-labelled.")
    human = await session.scalar(
        select(Annotation.id)
        .where(Annotation.item_id == item.id, Annotation.source == AnnotationSource.HUMAN)
        .limit(1)
    )
    if human is not None:
        raise ConflictError("The item already has a human annotation; pre-labels never replace it.")

    project = await session.get(Project, item.project_id)
    assert project is not None  # the item's foreign key guarantees it
    try:
        schema_version = await resolve_schema_version(
            session, project, payload.label_schema_version_id
        )
    except LookupError as exc:
        raise ConflictError(str(exc)) from exc
    if schema_version.label_schema_id != project.label_schema_id:
        raise ConflictError("The label schema version does not belong to this project.")

    annotation = await create_version(
        session,
        item=item,
        result=payload.result,
        label_schema_version_id=schema_version.id,
        author_model_version_id=version.id,
        status=AnnotationStatus.DRAFT,
    )
    if item.status is ItemStatus.NEW:
        item.status = next_status(item.status, Trigger.PRELABEL, role=None)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="annotation.prelabel",
        target_type="annotation",
        target_id=annotation.id,
        after={"version": annotation.version, "model_version_id": str(version.id)},
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(annotation)
    return annotation


@router.post(
    "/annotations/{annotation_id}/submit",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=AnnotationRead,
    summary="Submit a draft for review",
)
async def submit_annotation(
    annotation_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Annotation:
    """Promote the caller's own draft to `submitted`, validating it first.

    A submitted version is not edited in place either: a later change appends a
    new version, so the submitted state stays in the history.
    """
    annotation = await get_or_404(session, Annotation, annotation_id)
    item, role = await _item_and_role(session, annotation.item_id, current_user)
    config = await load_workflow(session, item.project_id)

    if annotation.status is not AnnotationStatus.DRAFT:
        raise ConflictError(
            f"Only a draft can be submitted; this version is {annotation.status.value!r}."
        )
    if annotation.author_user_id != current_user.id and not current_user.is_superuser:
        raise ForbiddenError("You can only submit your own annotation.")

    result = AnnotationResult.model_validate(annotation.result)
    # The draft's own task, if any (CONTRACTS.md *task*): a consensus / gold
    # / region save already carries its `task_id`, so the submit closes the
    # same task the draft was made under (region results are already merged
    # at draft time, so no second merge here).
    task = await session.get(Task, annotation.task_id) if annotation.task_id else None
    # Re-create rather than mutate: DATA-1 forbids updating a version in place,
    # and the new row carries the QA-6 validation and its own outbox event.
    submitted = await create_version(
        session,
        item=item,
        result=result,
        label_schema_version_id=annotation.label_schema_version_id,
        author_user_id=current_user.id,
        task_id=annotation.task_id,
        duration_ms=annotation.duration_ms,
        status=AnnotationStatus.SUBMITTED,
        kind=task_kind(task),
    )

    await submit_item(session, item=item, role=role, config=config, annotation=submitted, task=task)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="annotation.submit",
        target_type="annotation",
        target_id=submitted.id,
        after={"status": submitted.status.value, "version": submitted.version},
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(submitted)
    return submitted


@router.post(
    "/annotations/{annotation_id}/review",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=AnnotationRead,
    summary="Approve, reject, or correct a submitted annotation",
)
async def review_annotation(
    annotation_id: UUID,
    payload: Annotated[ReviewRequest, Body()],
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> Annotation:
    """Record a reviewer's verdict (WF-4).

    Approving with `corrected_result` stores the correction as a new version
    authored by the reviewer, so the annotator's original stays intact and the
    difference between them is recoverable.
    """
    annotation = await get_or_404(session, Annotation, annotation_id)
    item, role = await _item_and_role(session, annotation.item_id, current_user)
    config = await load_workflow(session, item.project_id)

    outcome = await apply_verdict(
        session,
        annotation=annotation,
        item=item,
        role=role,
        config=config,
        reviewer_id=current_user.id,
        organization_id=current_user.organization_id,
        approve=payload.approve,
        comment=payload.comment,
        corrected_result=payload.corrected_result,
        ip=client_ip,
    )

    await session.commit()
    await session.refresh(outcome)
    return outcome


@router.get(
    "/items/{item_id}/annotations/diff",
    response_model=dict[str, list[str]],
    summary="Shape-level diff between two versions",
)
async def diff_versions(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    base: int | None = None,
    compare: int | None = None,
) -> dict[str, list[str]]:
    """Which shapes were added, removed or changed between two versions (WF-4).

    Defaults to the two most recent versions, which is what a reviewer wants
    when looking at what an annotator changed after a rejection.
    """
    await _item_and_role(session, item_id, current_user)

    async def version_or_404(number: int) -> Annotation:
        found = await session.scalar(
            select(Annotation).where(Annotation.item_id == item_id, Annotation.version == number)
        )
        if found is None:
            raise NotFoundError(f"Item {item_id} has no version {number}.")
        return found

    if compare is None:
        newest = await latest_version(session, item_id)
        if newest is None:
            raise NotFoundError(f"Item {item_id} has no annotations.")
        compare = newest.version
    if base is None:
        base = max(compare - 1, 1)

    before = await version_or_404(base)
    after = await version_or_404(compare)

    return diff_shapes(
        AnnotationResult.model_validate(before.result),
        AnnotationResult.model_validate(after.result),
    )


__all__ = ["AnnotationCreate", "AnnotationRead", "ReviewRequest", "router"]
