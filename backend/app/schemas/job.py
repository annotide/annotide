"""Request/response DTOs for the job entity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import BaseSchema
from app.schemas.item import ItemStatus


class JobType(StrEnum):
    """Kind of background work a job performs."""

    SCAN_SOURCE = "scan_source"
    TILE_IMAGE = "tile_image"
    PRELABEL = "prelabel"
    EXPORT = "export"
    SNAPSHOT = "snapshot"
    IMPORT = "import"
    THUMBNAIL = "thumbnail"
    REBUILD_CACHE = "rebuild_cache"
    EXTRACT_TEXT = "extract_text"


class JobStatus(StrEnum):
    """Lifecycle state of a job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCreate(BaseSchema):
    """Payload to queue a new job."""

    project_id: UUID | None = None
    type: JobType
    payload: dict[str, Any] = Field(default_factory=dict)


class JobUpdate(BaseSchema):
    """Partial update payload for a job; all fields optional."""

    status: JobStatus | None = None
    progress: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


#: The only statuses a pre-labelling run may select (ML-10).
PRELABEL_STATUSES: frozenset[str] = frozenset({ItemStatus.NEW.value, ItemStatus.PRELABELED.value})


class PrelabelFilter(BaseSchema):
    """Which items a pre-labelling job covers (ML-2).

    Defaults to the items a customer would normally want touched: unstarted
    and previously pre-labelled ones. Items with a human annotation version
    are never selected, whichever statuses are listed here (ML-10).
    """

    item_status: list[ItemStatus] = Field(
        default_factory=lambda: [ItemStatus.NEW, ItemStatus.PRELABELED]
    )
    path_prefix: str | None = None

    @field_validator("item_status")
    @classmethod
    def _only_untouched(cls, statuses: list[ItemStatus]) -> list[ItemStatus]:
        # An item in any later state is, or has been, in a human's hands —
        # including `annotating` with a draft not yet saved (ML-10).
        bad = sorted({s.value for s in statuses} - PRELABEL_STATUSES)
        if bad:
            raise ValueError(f"prelabel may only target new or prelabeled items, not {bad}")
        if not statuses:
            raise ValueError("item_status must not be empty")
        return statuses


class PrelabelRequest(BaseSchema):
    """Body for queuing a pre-labelling job (ML-2).

    `limit` caps how many items are sent to the model in this run — a dry
    run over a handful of items before committing to the whole project
    (BYOM-7).
    """

    model_version_id: UUID
    label_schema_version_id: UUID | None = None
    filter: PrelabelFilter = Field(default_factory=PrelabelFilter)
    limit: int | None = Field(default=None, gt=0)
    confidence_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    #: ML-6: set each scored item's annotate-task priority from the
    #: prediction's uncertainty so the queue serves doubtful items first.
    prioritize_uncertain: bool = False


class JobRead(BaseSchema):
    """Job as returned by the API, e.g. `GET /jobs/{id}` for status and progress."""

    id: UUID
    project_id: UUID | None
    type: JobType
    status: JobStatus
    progress: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CacheRebuildRequest(BaseSchema):
    """Body of `POST /projects/{id}/cache/rebuild` (SRC-6)."""

    #: Delete each item's old thumbnail and tiles before regenerating them.
    purge: bool = False


class ExtractTextRequest(BaseSchema):
    """Body of `POST /projects/{id}/extract-text` (CONTRACTS.md *PDF text mode*)."""

    #: Only these items; all of the project's PDF text items when omitted.
    item_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=1000)
    #: Re-extract `ready` items too, those without annotation versions.
    force: bool = False


class ImportStatus(StrEnum):
    """Annotation status an import writes (a subset of `annotation_status`)."""

    SUBMITTED = "submitted"
    DRAFT = "draft"


class ImportRequest(BaseSchema):
    """Body for queuing an import job (EXP-6).

    `path` names the file, `.zip` archive or `/`-terminated prefix on
    `connector_id` (default: the project's source connector). `dry_run`
    parses and matches without writing anything — the preview to run first.
    """

    format: str
    path: str = Field(min_length=1)
    connector_id: UUID | None = None
    class_mapping: dict[str, str] = Field(default_factory=dict)
    #: `{schema_class | "*": {source_attribute: schema_attribute | None}}`,
    #: keyed by the shape's *mapped* class; `None` discards the attribute.
    attribute_mapping: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    status: ImportStatus = ImportStatus.SUBMITTED
    dry_run: bool = False
    label_schema_version_id: UUID | None = None
