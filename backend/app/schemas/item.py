"""Request/response DTOs for the item entity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import BaseSchema


class MediaType(StrEnum):
    """Kind of media an item holds."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    PDF = "pdf"
    #: LLM evaluation data: a conversation and candidate responses (§5).
    LLM = "llm"
    #: A CSV of channels over a time axis (§5 time series).
    TIMESERIES = "timeseries"


class ItemStatus(StrEnum):
    """Lifecycle state of an item, per the workflow state machine (§7)."""

    NEW = "new"
    PRELABELED = "prelabeled"
    ANNOTATING = "annotating"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


#: At most this many companion views per item (§5 multimodal).
MAX_VIEWS = 10


def validate_views(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """`meta.views`: up to `MAX_VIEWS` `{path, label?}` entries with distinct paths;
    `meta.pdf_text` is refused."""
    if meta is None:
        return meta
    if "pdf_text" in meta:
        # Written only by the scan and `extract_text`: it says which object on
        # the result connector is this item's text (CONTRACTS.md *PDF text mode*).
        raise ValueError("meta.pdf_text is set by the platform, not by clients")
    if "views" not in meta:
        return meta
    views = meta["views"]
    if not isinstance(views, list) or len(views) > MAX_VIEWS:
        raise ValueError(f"meta.views must be a list of at most {MAX_VIEWS} views")
    paths: set[str] = set()
    for view in views:
        if not isinstance(view, dict) or not isinstance(view.get("path"), str) or not view["path"]:
            raise ValueError("every view needs a non-empty 'path'")
        label = view.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 100):
            raise ValueError("a view label is text of at most 100 characters")
        if set(view) - {"path", "label"}:
            raise ValueError("a view has only 'path' and 'label'")
        if view["path"] in paths:
            raise ValueError(f"view {view['path']!r} is listed twice")
        paths.add(view["path"])
    return meta


class ItemCreate(BaseSchema):
    """Payload to register a new item under a project (project id comes from the route)."""

    connector_id: UUID
    path: str
    media_type: MediaType
    etag: str | None = None
    size_bytes: int
    width: int | None = None
    height: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    _views = field_validator("meta")(validate_views)


class ItemUpdate(BaseSchema):
    """Partial update payload for an item; all fields optional."""

    connector_id: UUID | None = None
    path: str | None = None
    media_type: MediaType | None = None
    etag: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    meta: dict[str, Any] | None = None
    status: ItemStatus | None = None

    _views = field_validator("meta")(validate_views)


class ItemRead(BaseSchema):
    """Item as returned by the API, with an optional short-lived signed media URL."""

    id: UUID
    project_id: UUID
    connector_id: UUID
    path: str
    media_type: MediaType
    etag: str | None
    size_bytes: int
    width: int | None
    height: int | None
    meta: dict[str, Any]
    status: ItemStatus
    created_at: datetime
    updated_at: datetime
    media_url: str | None = None
    thumbnail_url: str | None = None


class ItemView(BaseSchema):
    """`GET /items/{id}/views`: one companion view, signed (§5 multimodal)."""

    path: str
    label: str | None = None
    media_type: MediaType | None = None
    url: str | None = None
