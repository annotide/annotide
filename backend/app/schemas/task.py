"""Request/response DTOs for the task entity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class TaskType(StrEnum):
    """What kind of work the task represents."""

    ANNOTATE = "annotate"
    REVIEW = "review"


class TaskStatus(StrEnum):
    """Lifecycle state of a task."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskCreate(BaseSchema):
    """Payload to create/assign a task for an item (project id comes from the route)."""

    item_id: UUID
    type: TaskType
    assignee_id: UUID | None = None
    priority: int = 0
    deadline: datetime | None = None
    #: Explicit consensus slot (QA-1). When omitted on an `annotate` task in a
    #: project with `consensus_annotators` > 1, the system opens all N slots
    #: instead of one ordinary task (CONTRACTS.md *task*).
    slot: int | None = Field(default=None, ge=0)


class TaskUpdate(BaseSchema):
    """Partial update for a live task (WF-6): only the fields present in the body change.

    `deadline: null` / `assignee_id: null` clear the field; leaving a key out
    leaves it alone (see `model_fields_set`). Status and lock fields are not
    settable here — they move through claim / release / complete only.
    """

    priority: int | None = None
    deadline: datetime | None = None
    assignee_id: UUID | None = None


class TaskRead(BaseSchema):
    """Task as returned by the API."""

    id: UUID
    item_id: UUID
    project_id: UUID
    type: TaskType
    assignee_id: UUID | None
    status: TaskStatus
    locked_by_id: UUID | None
    locked_until: datetime | None
    priority: int
    deadline: datetime | None
    # Parallel annotate tasks (CONTRACTS.md *task*): consensus replica index
    # (QA-1), image region `[x_min, y_min, x_max, y_max]` (IMG-6), gold task (QA-4).
    slot: int | None = None
    region: tuple[float, float, float, float] | None = None
    gold: bool = False
    created_at: datetime
    updated_at: datetime
