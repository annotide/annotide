"""Response DTO for the audit log (SEC-3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.common import BaseSchema


class AuditEventRead(BaseSchema):
    """One append-only audit row."""

    id: UUID
    organization_id: UUID
    actor_id: UUID | None
    action: str
    target_type: str
    target_id: UUID | None
    ip: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime
