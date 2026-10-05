"""Project, label schema and label schema version tables."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single annotation project within an organization."""

    __tablename__ = "project"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # use_alter breaks the project <-> label_schema table creation cycle: this
    # foreign key is added via ALTER TABLE after label_schema exists.
    label_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey(
            "label_schema.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_project_label_schema_id",
        ),
        nullable=True,
        index=True,
    )
    source_connector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("connector.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_connector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("connector.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Where derived data (`cache/`) lives; `None` means the result connector (SRC-6).
    cache_connector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("connector.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_glob: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    settings: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)

    @property
    def effective_cache_connector_id(self) -> uuid.UUID | None:
        """The connector `cache/` is written to and signed from (SRC-6)."""
        return self.cache_connector_id or self.result_connector_id


class LabelSchema(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, versioned label schema owned by a project."""

    __tablename__ = "label_schema"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class LabelSchemaVersion(UUIDPrimaryKeyMixin, Base):
    """An immutable version of a label schema's JSON definition."""

    __tablename__ = "label_schema_version"
    __table_args__ = (
        Index(
            "uq_label_schema_version_schema_version",
            "label_schema_id",
            "version",
            unique=True,
        ),
    )

    label_schema_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("label_schema.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
