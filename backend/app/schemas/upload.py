"""Request/response DTOs for browser uploads to a project's source connector (§12)."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import BaseSchema

MAX_UPLOAD_FILES = 500


class UploadFileSpec(BaseSchema):
    """One file the browser wants to upload. `path` is relative to the project's source prefix."""

    path: str = Field(min_length=1, max_length=1024)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)


class UploadUrlsRequest(BaseSchema):
    """Body for `POST /projects/{id}/uploads`."""

    files: list[UploadFileSpec] = Field(min_length=1, max_length=MAX_UPLOAD_FILES)


class UploadTarget(BaseSchema):
    """Where and how to PUT one file. `path` is the object path the item will be registered at."""

    path: str
    url: str
    method: str = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)


class UploadUrlsResponse(BaseSchema):
    """One write-scoped signed URL per requested file."""

    prefix: str
    uploads: list[UploadTarget]
