"""Bulk operations on a project's items (WF-8).

One endpoint, `POST /projects/{id}/items/bulk`, takes a list of item ids and
one action. Each item is handled on its own: the ones the action does not
apply to are reported in `skipped` with a reason rather than failing the
whole request, so an owner can select a mixed page of items and approve
"whatever is submitted" in one click.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema
from app.schemas.task import TaskType

#: Cap per request: enough for a page of the item grid, small enough that a
#: single transaction stays short.
BULK_MAX_ITEMS = 500

ItemIds = Annotated[list[UUID], Field(min_length=1, max_length=BULK_MAX_ITEMS)]
Tag = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[^\s,]+$")]


class BulkAssign(BaseSchema):
    """Point the items' live `type` tasks at `assignee_id` (or nobody), and /
    or set their priority and deadline. An item without a live task of that
    type gets one opened when its status allows annotation (`annotate`) or
    review (`review`); items being worked on right now (`in_progress`) are
    skipped rather than pulled from under the annotator."""

    action: Literal["assign"]
    item_ids: ItemIds
    type: TaskType = TaskType.ANNOTATE
    #: Present → set; `null` → unassign; absent → leave as is.
    assignee_id: UUID | None = None
    priority: int | None = None
    deadline: datetime | None = None


class BulkReturn(BaseSchema):
    """Return the items' `in_progress` tasks to the open queue: the lock is
    dropped and the assignee cleared, as if the holder had released it."""

    action: Literal["return"]
    item_ids: ItemIds


class BulkApprove(BaseSchema):
    """Approve the latest `submitted` version of each item (WF-4 verdict
    without a correction). Items not awaiting review are skipped."""

    action: Literal["approve"]
    item_ids: ItemIds
    comment: str | None = None


class BulkReject(BaseSchema):
    """Reject the latest `submitted` version of each item (WF-4) with one
    shared `comment`, which every item's thread gets — a rejection always
    tells the annotator why. Items not awaiting review are skipped."""

    action: Literal["reject"]
    item_ids: ItemIds
    comment: str = Field(min_length=1, max_length=10_000)


class BulkTag(BaseSchema):
    """Add and / or remove free-form tags on the items (`item.meta.tags`)."""

    action: Literal["tag"]
    item_ids: ItemIds
    add: list[Tag] = Field(default_factory=list)
    remove: list[Tag] = Field(default_factory=list)


BulkRequest = Annotated[
    BulkAssign | BulkReturn | BulkApprove | BulkReject | BulkTag,
    Field(discriminator="action"),
]


class BulkSkipped(BaseSchema):
    """One item the action did not apply to, and why."""

    item_id: UUID
    reason: str


class BulkResult(BaseSchema):
    """Outcome of a bulk request: how many items changed, and which did not."""

    applied: int
    skipped: list[BulkSkipped]
