"""GDPR access and erasure DTOs (SEC-6). See docs/CONTRACTS.md → "### user"."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class PersonalProfile(BaseSchema):
    """The user row without credentials: flags stand in for secrets."""

    id: UUID
    organization_id: UUID
    email: str
    display_name: str
    is_active: bool
    is_superuser: bool
    is_service: bool
    idp_linked: bool
    mfa_enabled: bool
    last_seen_at: datetime | None
    erased_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PersonalMembership(BaseSchema):
    project_id: UUID
    project_name: str
    role: str
    created_at: datetime


class PersonalApiKey(BaseSchema):
    id: UUID
    name: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class PersonalComment(BaseSchema):
    id: UUID
    project_id: UUID
    item_id: UUID | None
    annotation_id: UUID | None
    body: str
    created_at: datetime
    resolved_at: datetime | None


class PersonalAnnotation(BaseSchema):
    """Metadata only: the result is project content, not personal data."""

    id: UUID
    item_id: UUID
    version: int
    status: str
    kind: str
    duration_ms: int | None
    created_at: datetime


class PersonalTask(BaseSchema):
    id: UUID
    project_id: UUID
    item_id: UUID
    type: str
    status: str


class PersonalNotification(BaseSchema):
    id: UUID
    type: str
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class PersonalAuditEvent(BaseSchema):
    id: UUID
    action: str
    target_type: str
    target_id: UUID | None
    ip: str | None
    created_at: datetime


class PersonalDataExport(BaseSchema):
    """Everything the platform holds about one person (SEC-6 access)."""

    generated_at: datetime
    user: PersonalProfile
    memberships: list[PersonalMembership] = Field(default_factory=list)
    api_keys: list[PersonalApiKey] = Field(default_factory=list)
    comments: list[PersonalComment] = Field(default_factory=list)
    annotations: list[PersonalAnnotation] = Field(default_factory=list)
    tasks: list[PersonalTask] = Field(default_factory=list)
    notifications: list[PersonalNotification] = Field(default_factory=list)
    audit_events: list[PersonalAuditEvent] = Field(default_factory=list)


class EraseRequest(BaseSchema):
    """`confirm_email` must repeat the user's current e-mail, as a guard."""

    confirm_email: str
    redact_comments: bool = False
