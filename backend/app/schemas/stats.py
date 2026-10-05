"""Response DTOs for `GET /projects/{id}/stats` (UX-5).

Plain counts the dashboard renders as-is. Enum keys are the string values of
the corresponding `item_status` / `task_status` / `annotation_status` enums,
spelled out here so this module stays free of `app.models`.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class ItemStats(BaseSchema):
    total: int
    by_status: dict[str, int]


class TaskTypeStats(BaseSchema):
    open: int = 0
    in_progress: int = 0
    done: int = 0
    cancelled: int = 0


class TaskStats(BaseSchema):
    annotate: TaskTypeStats
    review: TaskTypeStats


class AnnotationStats(BaseSchema):
    versions: int
    by_source: dict[str, int]
    latest_by_status: dict[str, int]


class ReviewStats(BaseSchema):
    approved: int
    rejected: int
    rejection_rate: float = Field(ge=0.0, le=1.0)


class ThroughputDay(BaseSchema):
    day: date
    submitted: int = 0
    approved: int = 0
    rejected: int = 0


class ClassCount(BaseSchema):
    label: str
    count: int


class AnnotatorStats(BaseSchema):
    user_id: UUID
    display_name: str
    submitted: int = 0
    approved: int = 0
    rejected: int = 0


class ProjectStats(BaseSchema):
    items: ItemStats
    tasks: TaskStats
    annotations: AnnotationStats
    review: ReviewStats
    throughput: list[ThroughputDay]
    classes: list[ClassCount]
    annotators: list[AnnotatorStats]
