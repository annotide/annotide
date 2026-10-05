"""Task listing, assignment, prioritisation, claiming and locking (WF-2, WF-3, WF-6).

The claim/lock invariants live in `app.services.tasks`; this router only
translates HTTP into those calls and into the shared pagination and
membership helpers, per house style (see `api/v1/annotations.py`).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import (
    UNRESTRICTED_LICENCE,
    CurrentUserDep,
    PageParamsDep,
    SessionDep,
    SettingsDep,
)
from app.api.errors import ApiError, ConflictError, ForbiddenError, NotFoundError
from app.models import Item, Task
from app.models import TaskStatus as ModelTaskStatus
from app.models import TaskType as ModelTaskType
from app.schemas import Page, TaskCreate, TaskRead, TaskUpdate
from app.schemas import TaskStatus as SchemaTaskStatus
from app.schemas import TaskType as SchemaTaskType
from app.services.gold import open_due_gold_task
from app.services.item_flow import load_workflow
from app.services.repository import (
    ensure_item_member,
    ensure_project_member,
    get_or_404,
    member_prefixes,
    paginate,
    path_scope_clause,
)
from app.services.tasks import (
    claim_next_task,
    ensure_assignee_member,
    extend_lock,
    open_annotate_tasks,
    open_task,
    release_task,
    update_task,
)

router = APIRouter(tags=["tasks"])

#: Roles allowed to hand out work. Matches the `_REVIEWER`-and-`owner` shape
#: used throughout `app.services.workflow`, kept local since this is an
#: assignment permission, not an item-state transition.
_CAN_ASSIGN = frozenset({"owner", "reviewer"})


class ClaimRequest(BaseModel):
    """Optional JSON body for `POST /tasks/next`, alternative to the query parameter."""

    project_id: UUID | None = None
    type: SchemaTaskType | None = None


class ExtendRequest(BaseModel):
    """Optional JSON body for `POST /tasks/{task_id}/extend`."""

    lock_ttl_seconds: int | None = Field(default=None, gt=0)


@router.get(
    "/projects/{project_id}/tasks",
    response_model=Page[TaskRead],
    summary="List tasks in a project",
)
async def list_tasks(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page: PageParamsDep,
    task_status: Annotated[SchemaTaskStatus | None, Query(alias="status")] = None,
    task_type: Annotated[SchemaTaskType | None, Query(alias="type")] = None,
    assignee_id: Annotated[UUID | None, Query()] = None,
) -> Page[TaskRead]:
    """List a project's tasks, cursor-paginated and filterable by status/type/assignee."""
    await ensure_project_member(session, project_id, current_user)

    stmt = select(Task).where(Task.project_id == project_id)
    prefixes = await member_prefixes(session, project_id, current_user)
    if prefixes:
        stmt = stmt.where(
            Task.item_id.in_(select(Item.id).where(path_scope_clause(Item.path, prefixes)))
        )
    if task_status is not None:
        stmt = stmt.where(Task.status == ModelTaskStatus(task_status.value))
    if task_type is not None:
        stmt = stmt.where(Task.type == ModelTaskType(task_type.value))
    if assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == assignee_id)

    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(Task.created_at, Task.id),
        key_of=lambda task: (task.created_at, task.id),
    )
    return Page[TaskRead](
        items=[TaskRead.model_validate(task) for task in result.items],
        next_cursor=result.next_cursor,
    )


@router.post(
    "/projects/{project_id}/tasks",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create and assign a task",
)
async def create_task(
    project_id: UUID,
    payload: TaskCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> TaskRead:
    """Create a task on an item, assigning work to an annotator or reviewer.

    Restricted to the project's owner or a reviewer: creating a task assigns
    work to someone else, which is a different permission from doing the work
    itself (any member can claim from the open queue via `POST /tasks/next`).
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role not in _CAN_ASSIGN:
        raise ForbiddenError("Only a project owner or reviewer may create tasks.")
    target = await session.get(Item, payload.item_id)
    # A foreign or unknown item is the same 404: a task must
    # never point at another project's item, nor reveal that it exists.
    if target is None or target.project_id != project_id:
        raise NotFoundError(f"Item {payload.item_id} does not exist.")
    await ensure_item_member(session, target, current_user)
    await ensure_assignee_member(session, project_id, payload.assignee_id)

    task_type = ModelTaskType(payload.type.value)
    if task_type is ModelTaskType.ANNOTATE and payload.slot is None:
        # QA-1: the first annotate task of an item fans out to N consensus
        # slots when the project asks for them.
        config = await load_workflow(session, project_id)
        opened = await open_annotate_tasks(
            session,
            item_id=payload.item_id,
            project_id=project_id,
            config=config,
            assignee_id=payload.assignee_id,
            priority=payload.priority,
            deadline=payload.deadline,
        )
        task = opened[0] if opened else None
    else:
        task = await open_task(
            session,
            item_id=payload.item_id,
            project_id=project_id,
            task_type=task_type,
            assignee_id=payload.assignee_id,
            priority=payload.priority,
            deadline=payload.deadline,
            slot=payload.slot,
        )
    if task is None:
        # The queue must hold one live task per item and type (WF-2); to
        # re-prioritise or re-assign the existing one, PATCH it instead.
        raise ConflictError(
            f"Item {payload.item_id} already has an open or in-progress {payload.type.value} task."
        )
    await session.commit()
    await session.refresh(task)
    return TaskRead.model_validate(task)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskRead,
    summary="Re-prioritise, re-schedule or re-assign a task",
)
async def patch_task(
    task_id: UUID,
    payload: TaskUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> TaskRead:
    """Change a live task's `priority`, `deadline` or `assignee_id` (WF-6).

    Owner or reviewer only, like `POST /projects/{id}/tasks`: this decides
    who works on what and when. Only keys present in the body change, so
    `{"deadline": null}` clears the deadline while `{}` is a no-op. A `done`
    or `cancelled` task, or reassigning one somebody is working on, is a 409.
    """
    task = await get_or_404(session, Task, task_id)
    role = await ensure_item_member(
        session, await get_or_404(session, Item, task.item_id), current_user
    )
    if role not in _CAN_ASSIGN:
        raise ForbiddenError("Only a project owner or reviewer may change tasks.")

    fields = payload.model_dump(include=payload.model_fields_set)
    if fields.get("assignee_id") != task.assignee_id:
        await ensure_assignee_member(session, task.project_id, fields.get("assignee_id"))
    updated = await update_task(session, task, **fields)
    return TaskRead.model_validate(updated)


@router.post(
    "/tasks/next",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=TaskRead,
    responses={204: {"description": "No open task is available right now."}},
    summary="Claim the next open task",
)
async def claim_next(
    session: SessionDep,
    settings: SettingsDep,
    current_user: CurrentUserDep,
    project_id: Annotated[UUID | None, Query()] = None,
    task_type: Annotated[SchemaTaskType | None, Query(alias="type")] = None,
    payload: ClaimRequest | None = None,
) -> TaskRead | Response:
    """Claim the highest-priority open task in a project (WF-2, WF-3).

    `project_id` may come from the query string or the JSON body — whichever
    is given; the query parameter wins if both are present. `type` narrows the
    claim to `annotate` or `review` tasks, also accepted as either the query
    parameter or the JSON body's `type` field with the same query-wins rule;
    omitted, either kind of task is eligible. Claiming a task also takes its
    lock (see `app.services.tasks.claim_next_task` for the race this guards
    against and why it is safe under concurrent claims).

    An empty queue is a normal state, not an error: returns `204 No Content`
    rather than `404`.
    """
    resolved_project_id = project_id or (payload.project_id if payload is not None else None)
    if resolved_project_id is None:
        raise ApiError("project_id is required, as a query parameter or a JSON body field.")
    resolved_type = task_type or (payload.type if payload is not None else None)

    await ensure_project_member(session, resolved_project_id, current_user)
    if resolved_type is None or resolved_type is SchemaTaskType.ANNOTATE:
        config = await load_workflow(session, resolved_project_id)
        if config.gold_every is not None:
            # QA-4: every `gold_every`-th piece of annotate work is a gold task.
            await open_due_gold_task(
                session,
                project_id=resolved_project_id,
                user_id=current_user.id,
                every=config.gold_every,
            )
    task = await claim_next_task(
        session,
        resolved_project_id,
        current_user.id,
        settings.task_lock_ttl,
        task_type=ModelTaskType(resolved_type.value) if resolved_type is not None else None,
        path_prefixes=await member_prefixes(session, resolved_project_id, current_user),
    )
    if task is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return TaskRead.model_validate(task)


@router.post(
    "/tasks/{task_id}/release",
    response_model=TaskRead,
    summary="Release the lock on a task",
)
async def release(
    task_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> TaskRead:
    """Release `task_id`'s lock, returning it to the open queue.

    Only the current lock holder or a superuser may release someone else's
    lock; anyone else gets `409 task-locked` (see
    `app.services.tasks.release_task`).
    """
    task = await get_or_404(session, Task, task_id)
    await ensure_project_member(session, task.project_id, current_user)
    released = await release_task(
        session, task, current_user.id, is_superuser=current_user.is_superuser
    )
    return TaskRead.model_validate(released)


@router.post(
    "/tasks/{task_id}/extend",
    response_model=TaskRead,
    summary="Extend (heartbeat) the lock on a task",
)
async def extend(
    task_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    current_user: CurrentUserDep,
    payload: ExtendRequest | None = None,
) -> TaskRead:
    """Push a held lock's expiry forward for a long-running annotation.

    Only the current lock holder may extend; anyone else gets `409
    task-locked`. `lock_ttl_seconds` defaults to `settings.task_lock_ttl` when
    omitted.
    """
    task = await get_or_404(session, Task, task_id)
    await ensure_project_member(session, task.project_id, current_user)
    ttl = payload.lock_ttl_seconds if payload is not None and payload.lock_ttl_seconds else None
    extended = await extend_lock(session, task, current_user.id, ttl or settings.task_lock_ttl)
    return TaskRead.model_validate(extended)


__all__ = ["ClaimRequest", "ExtendRequest", "router"]
