"""Notification table (WF-5).

An in-app notification for a user, raised by a mention, a reply on a thread
they started, or a review verdict on their annotation. Also e-mailed when SMTP
is configured and the user has not opted out (API-7,
`services/notification_email.py`).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class NotificationType(enum.StrEnum):
    """Kind of in-app notification (``notification_type`` PG enum)."""

    MENTION = "mention"
    REPLY = "reply"
    REVIEW = "review"


class Notification(UUIDPrimaryKeyMixin, Base):
    """An in-app notification for a user (WF-5)."""

    __tablename__ = "notification"
    __table_args__ = (Index("ix_notification_user_id_read_at", "user_id", "read_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        pg_enum(NotificationType, "notification_type"), nullable=False
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set once the e-mail copy went out (API-7); attempts cap retries.
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
