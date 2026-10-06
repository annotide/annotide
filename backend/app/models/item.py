"""Item and task tables."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class MediaType(enum.StrEnum):
    """Kind of media an item points at (``media_type`` PG enum)."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    PDF = "pdf"
    #: LLM evaluation data: a conversation and candidate responses (§5).
    LLM = "llm"
    #: A CSV of channels over a time axis (§5 time series).
    TIMESERIES = "timeseries"


class ItemStatus(enum.StrEnum):
    """Item workflow state (``item_status`` PG enum), matches the §7 state machine."""

    NEW = "new"
    PRELABELED = "prelabeled"
    ANNOTATING = "annotating"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class TaskType(enum.StrEnum):
    """Kind of work a task represents (``task_type`` PG enum)."""

    ANNOTATE = "annotate"
    REVIEW = "review"


class TaskStatus(enum.StrEnum):
    """Task workflow state (``task_status`` PG enum)."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single source object (image, video, ...) scanned from a connector."""

    __tablename__ = "item"
    __table_args__ = (
        Index(
            "uq_item_project_connector_path",
            "project_id",
            "connector_id",
            "path",
            unique=True,
        ),
        Index("ix_item_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("connector.id"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[MediaType] = mapped_column(pg_enum(MediaType, "media_type"), nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    status: Mapped[ItemStatus] = mapped_column(
        pg_enum(ItemStatus, "item_status"), nullable=False, default=ItemStatus.NEW
    )
    #: Path of the generated thumbnail on the project's *result* connector (IMG-8).
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A unit of work (annotate or review) on a single item."""

    __tablename__ = "task"
    __table_args__ = (
        Index(
            "ix_task_project_id_status_assignee_id",
            "project_id",
            "status",
            "assignee_id",
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[TaskType] = mapped_column(pg_enum(TaskType, "task_type"), nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[TaskStatus] = mapped_column(
        pg_enum(TaskStatus, "task_status"), nullable=False, default=TaskStatus.OPEN
    )
    locked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Parallel annotate tasks on one item (QA-1, QA-4, IMG-6). All three are
    # null / false on an ordinary task; see CONTRACTS.md *task*.
    slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[list[float] | None] = mapped_column(JSONBType, nullable=True)
    gold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
