"""Shared base model, pagination envelope and error shape for the schema layer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    """Base configuration shared by every DTO in the schema layer.

    - `from_attributes` lets a model be built directly from an ORM instance.
    - `extra="forbid"` rejects unknown fields instead of silently dropping them.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Page[T](BaseSchema):
    """Cursor-paginated response envelope, per API-2: `{"items": [...], "next_cursor": ...}`."""

    items: list[T]
    next_cursor: str | None = None


class ProblemDetail(BaseSchema):
    """RFC 9457 problem details error body, per API-2."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
