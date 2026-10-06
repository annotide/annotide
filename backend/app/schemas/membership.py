"""Request/response DTOs for project membership (permissions, SEC-3)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import field_validator, model_validator

from app.schemas.common import BaseSchema


class ProjectRole(StrEnum):
    """A user's role on a single project (mirrors `app.models.ProjectRole`)."""

    OWNER = "owner"
    ANNOTATOR = "annotator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


MAX_PREFIXES = 20


def validate_prefixes(value: list[str] | None) -> list[str] | None:
    """Folder limits: 1-20 non-empty prefixes, no `..`; deduplicated and sorted."""
    if value is None:
        return None
    if not 1 <= len(value) <= MAX_PREFIXES:
        raise ValueError(f"path_prefixes must list 1-{MAX_PREFIXES} folders, or be null")
    for prefix in value:
        if not prefix or len(prefix) > 512:
            raise ValueError("a path prefix is 1-512 characters")
        if ".." in prefix.split("/"):
            raise ValueError("a path prefix must not contain '..'")
    return sorted(set(value))


class MemberRead(BaseSchema):
    """A project member, joined with the `user` row for display fields."""

    user_id: UUID
    email: str
    display_name: str
    role: ProjectRole
    #: `idp` when SSO group sync granted it (AUTH-3); editing the role makes it `manual`.
    source: Literal["manual", "idp"] = "manual"
    #: Folder-level access: only items under these prefixes; null = all.
    path_prefixes: list[str] | None = None
    created_at: datetime


class MemberCreate(BaseSchema):
    """Payload to add a member to a project — owner only.

    Exactly one of `user_id` / `email` identifies the user to add; the other
    must be omitted.
    """

    user_id: UUID | None = None
    email: str | None = None
    role: ProjectRole
    path_prefixes: list[str] | None = None

    _prefixes = field_validator("path_prefixes")(validate_prefixes)

    @model_validator(mode="after")
    def _validate_exactly_one_identifier(self) -> MemberCreate:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("Provide exactly one of user_id or email, not both or neither.")
        if self.role is ProjectRole.OWNER and self.path_prefixes is not None:
            raise ValueError("An owner sees the whole project; path_prefixes must be null.")
        return self


class MemberUpdate(BaseSchema):
    """Payload to change a member's role and / or folders — owner only; keys present change."""

    role: ProjectRole | None = None
    #: `null` gives the whole project back.
    path_prefixes: list[str] | None = None

    _prefixes = field_validator("path_prefixes")(validate_prefixes)
