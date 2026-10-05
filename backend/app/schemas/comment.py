"""Request/response DTOs for the comment entity (WF-5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class CommentCreate(BaseSchema):
    """Payload to add a comment to an item's thread."""

    body: str = Field(min_length=1, max_length=4000)
    annotation_id: UUID | None = None
    parent_id: UUID | None = None
    anchor: dict[str, Any] | None = None


class CommentResolve(BaseSchema):
    """Payload to resolve or reopen a comment."""

    resolved: bool


class CommentRead(BaseSchema):
    """Comment as returned by the API."""

    id: UUID
    project_id: UUID
    item_id: UUID | None
    annotation_id: UUID | None
    parent_id: UUID | None
    author_id: UUID
    body: str
    anchor: dict[str, Any] | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
