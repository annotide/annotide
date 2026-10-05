"""Job, snapshot, audit event and outbox event tables (operational concerns)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import InetType, JSONBType, UUIDType, pg_enum


class JobType(enum.StrEnum):
    """Kind of background job (``job_type`` PG enum)."""

    SCAN_SOURCE = "scan_source"
    TILE_IMAGE = "tile_image"
    PRELABEL = "prelabel"
    EXPORT = "export"
    SNAPSHOT = "snapshot"
    IMPORT = "import"
    THUMBNAIL = "thumbnail"
    REBUILD_CACHE = "rebuild_cache"
    EXTRACT_TEXT = "extract_text"


class JobStatus(enum.StrEnum):
    """Background job lifecycle state (``job_status`` PG enum)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tracked background job run by the arq worker."""

    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_job_progress_range"),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[JobType] = mapped_column(pg_enum(JobType, "job_type"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.QUEUED
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Snapshot(UUIDPrimaryKeyMixin, Base):
    """An immutable, point-in-time export of a project's annotations (EXP-1)."""

    __tablename__ = "snapshot"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    filter: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    #: Train / val / test partition config (`services/datasets.py::SplitConfig`, EXP-3);
    #: null when the snapshot was frozen without one.
    split: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    label_schema_version_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("label_schema_version.id"), nullable=False, index=True
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blob_path: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """An append-only audit log entry (SEC-3): no updates, no deletes."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organization.id"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    ip: Mapped[str | None] = mapped_column(InetType, nullable=True)
    before: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    after: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    """A transactional outbox row (DATA-2): written atomically with its aggregate.

    A worker publishes the payload to blob storage and stamps ``published_at``.
    """

    __tablename__ = "outbox_event"

    aggregate_type: Mapped[str] = mapped_column(String(255), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyKey(UUIDPrimaryKeyMixin, Base):
    """A consumed `Idempotency-Key` header (API-2).

    One row per (organisation, endpoint, key); `target_id` is the row the
    first request created, replayed to every retry. The unique constraint is
    what makes two concurrent retries collapse into one create.
    """

    __tablename__ = "idempotency_key"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "endpoint", "key", name="uq_idempotency_key_org_endpoint_key"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    #: Which create the key was used on: `project.create`, `job.export`, …
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
