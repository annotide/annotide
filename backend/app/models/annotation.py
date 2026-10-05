"""Annotation and comment tables."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class AnnotationSource(enum.StrEnum):
    """Who produced an annotation version (``annotation_source`` PG enum)."""

    HUMAN = "human"
    MODEL = "model"


class AnnotationStatus(enum.StrEnum):
    """Annotation review state (``annotation_status`` PG enum)."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class AnnotationKind(enum.StrEnum):
    """Which line of work a version belongs to (``annotation_kind`` PG enum).

    Only `primary` versions are the item's annotation; `consensus` (QA-1) and
    `gold` (QA-4) versions are per-annotator attempts kept beside it.
    """

    PRIMARY = "primary"
    CONSENSUS = "consensus"
    GOLD = "gold"


class Annotation(UUIDPrimaryKeyMixin, Base):
    """An immutable, versioned annotation result for an item (DATA-1).

    A change is never an update in place: it is a new row with ``version + 1``.
    """

    __tablename__ = "annotation"
    __table_args__ = (
        Index("uq_annotation_item_version", "item_id", "version", unique=True),
        Index("ix_annotation_item_id_kind", "item_id", "kind"),
        CheckConstraint(
            "(author_user_id IS NOT NULL) <> (author_model_version_id IS NOT NULL)",
            name="ck_annotation_author_xor",
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("task.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("model_version.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[AnnotationSource] = mapped_column(
        pg_enum(AnnotationSource, "annotation_source"), nullable=False
    )
    label_schema_version_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("label_schema_version.id"), nullable=False, index=True
    )
    result: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False)
    status: Mapped[AnnotationStatus] = mapped_column(
        pg_enum(AnnotationStatus, "annotation_status"),
        nullable=False,
        default=AnnotationStatus.DRAFT,
    )
    kind: Mapped[AnnotationKind] = mapped_column(
        pg_enum(AnnotationKind, "annotation_kind"),
        nullable=False,
        default=AnnotationKind.PRIMARY,
        server_default=AnnotationKind.PRIMARY.value,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blob_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Comment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A threaded comment attached to an item and/or an annotation."""

    __tablename__ = "comment"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("item.id", ondelete="SET NULL"), nullable=True, index=True
    )
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("annotation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("comment.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
