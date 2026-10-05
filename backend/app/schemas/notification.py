"""Response DTOs for in-app notifications (WF-5)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.schemas.common import BaseSchema


class NotificationType(StrEnum):
    """Mirrors the ``notification_type`` enum in the data model."""

    MENTION = "mention"
    REPLY = "reply"
    REVIEW = "review"


class NotificationRead(BaseSchema):
    """One notification as returned by the API."""

    id: UUID
    user_id: UUID
    type: NotificationType
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class UnreadCount(BaseSchema):
    """`GET /notifications/unread-count`."""

    count: int
