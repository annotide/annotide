"""Outbound webhooks (API-4) and their delivery log."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class WebhookFormat(enum.StrEnum):
    """Body a hook receives (``webhook_format`` PG enum, API-7)."""

    JSON = "json"
    SLACK = "slack"
    TEAMS = "teams"


class WebhookDeliveryStatus(enum.StrEnum):
    """Lifecycle of one delivery attempt series."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Webhook(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A subscriber URL for platform events (API-4).

    `project_id` null means every project in the organisation. `secret` is
    the HMAC key deliveries are signed with; it is shown once at creation and
    on rotation, never read back through the API.
    """

    __tablename__ = "webhook"
    __table_args__ = (
        Index("ix_webhook_organization_id_is_active", "organization_id", "is_active"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Event names this hook receives; `["*"]` subscribes to everything.
    events: Mapped[list[str]] = mapped_column(JSONBType, nullable=False, default=list)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `json` is the signed event document; `slack` / `teams` a chat message.
    format: Mapped[WebhookFormat] = mapped_column(
        pg_enum(WebhookFormat, "webhook_format"),
        nullable=False,
        default=WebhookFormat.JSON,
        server_default=WebhookFormat.JSON.value,
    )


class WebhookDelivery(UUIDPrimaryKeyMixin, Base):
    """One event bound for one webhook: written with the event, delivered by the worker."""

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        Index("ix_webhook_delivery_status_next_attempt_at", "status", "next_attempt_at"),
    )

    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("webhook.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        pg_enum(WebhookDeliveryStatus, "webhook_delivery_status"),
        nullable=False,
        default=WebhookDeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
