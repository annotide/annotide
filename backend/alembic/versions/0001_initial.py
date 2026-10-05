"""Initial schema: every table from docs/CONTRACTS.md §9.

Revision ID: 0001
Revises:
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Enum value lists, kept in lockstep with app.models (see e.g. app.models.item.ItemStatus).
_PROJECT_ROLE = ("owner", "annotator", "reviewer", "viewer")
_CONNECTOR_TYPE = ("azure_blob", "s3", "gcs", "local", "http")
_CONNECTOR_IDENTITY = (
    "managed_identity",
    "service_principal",
    "account_key",
    "sas_token",
    "iam_role",
    "access_key",
    "none",
)
_MEDIA_TYPE = ("image", "video", "audio", "text", "pdf")
_ITEM_STATUS = (
    "new",
    "prelabeled",
    "annotating",
    "submitted",
    "in_review",
    "approved",
    "rejected",
    "skipped",
)
_TASK_TYPE = ("annotate", "review")
_TASK_STATUS = ("open", "in_progress", "done", "cancelled")
_ANNOTATION_SOURCE = ("human", "model")
_ANNOTATION_STATUS = ("draft", "submitted", "approved", "rejected")
_MODEL_TASK = ("detect", "segment", "classify", "ner", "llm")
_JOB_TYPE = ("scan_source", "tile_image", "prelabel", "export", "snapshot")
_JOB_STATUS = ("queued", "running", "succeeded", "failed", "cancelled")


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')

    op.create_table(
        "organization",
        _uuid_pk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        *_timestamps(),
    )

    op.create_table(
        "user",
        _uuid_pk(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("idp_subject", sa.String(255), nullable=True, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "connector",
        _uuid_pk(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "type",
            sa.Enum(*_CONNECTOR_TYPE, name="connector_type", native_enum=True),
            nullable=False,
        ),
        sa.Column(
            "identity_type",
            sa.Enum(*_CONNECTOR_IDENTITY, name="connector_identity", native_enum=True),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        *_timestamps(),
    )

    op.create_table(
        "model",
        _uuid_pk(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "task", sa.Enum(*_MODEL_TASK, name="model_task", native_enum=True), nullable=False
        ),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("identity_type", sa.String(255), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        _created_at(),
    )

    op.create_table(
        "model_version",
        _uuid_pk(),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "class_mapping",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metrics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _created_at(),
    )

    # project.label_schema_id -> label_schema.id is added via ALTER TABLE below,
    # once the label_schema table exists (breaks the project <-> label_schema cycle).
    op.create_table(
        "project",
        _uuid_pk(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("label_schema_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "source_connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "result_connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("source_prefix", sa.Text(), nullable=True),
        sa.Column("source_glob", sa.Text(), nullable=True),
        sa.Column(
            "workflow", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        *_timestamps(),
    )

    op.create_table(
        "label_schema",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "label_schema_version",
        _uuid_pk(),
        sa.Column(
            "label_schema_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("label_schema.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        _created_at(),
    )
    op.create_index(
        "uq_label_schema_version_schema_version",
        "label_schema_version",
        ["label_schema_id", "version"],
        unique=True,
    )

    op.create_foreign_key(
        "fk_project_label_schema_id",
        "project",
        "label_schema",
        ["label_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "membership",
        _uuid_pk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role", sa.Enum(*_PROJECT_ROLE, name="project_role", native_enum=True), nullable=False
        ),
        _created_at(),
    )

    op.create_table(
        "item",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connector.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "media_type", sa.Enum(*_MEDIA_TYPE, name="media_type", native_enum=True), nullable=False
        ),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "status",
            sa.Enum(*_ITEM_STATUS, name="item_status", native_enum=True),
            nullable=False,
            server_default="new",
        ),
        *_timestamps(),
    )
    op.create_index(
        "uq_item_project_connector_path",
        "item",
        ["project_id", "connector_id", "path"],
        unique=True,
    )
    op.create_index("ix_item_project_id_status", "item", ["project_id", "status"])

    op.create_table(
        "task",
        _uuid_pk(),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("type", sa.Enum(*_TASK_TYPE, name="task_type", native_enum=True), nullable=False),
        sa.Column(
            "assignee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "status",
            sa.Enum(*_TASK_STATUS, name="task_status", native_enum=True),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "locked_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_task_project_id_status_assignee_id",
        "task",
        ["project_id", "status", "assignee_id"],
    )

    op.create_table(
        "annotation",
        _uuid_pk(),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "author_model_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_version.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "source",
            sa.Enum(*_ANNOTATION_SOURCE, name="annotation_source", native_enum=True),
            nullable=False,
        ),
        sa.Column(
            "label_schema_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("label_schema_version.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_ANNOTATION_STATUS, name="annotation_status", native_enum=True),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("blob_path", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "(author_user_id IS NOT NULL) <> (author_model_version_id IS NOT NULL)",
            name="ck_annotation_author_xor",
        ),
    )
    op.create_index("uq_annotation_item_version", "annotation", ["item_id", "version"], unique=True)

    op.create_table(
        "comment",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("item.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "annotation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("comment.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("anchor", postgresql.JSONB(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "snapshot",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "filter", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "label_schema_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("label_schema_version.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blob_path", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column(
            "created_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=False,
            index=True,
        ),
        _created_at(),
    )

    op.create_table(
        "job",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("type", sa.Enum(*_JOB_TYPE, name="job_type", native_enum=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_JOB_STATUS, name="job_status", native_enum=True),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="ck_job_progress_range"),
    )

    op.create_table(
        "audit_event",
        _uuid_pk(),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        _created_at(),
    )
    op.create_index(
        "ix_audit_event_organization_id_created_at",
        "audit_event",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "outbox_event",
        _uuid_pk(),
        sa.Column("aggregate_type", sa.String(255), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
    )


def downgrade() -> None:
    op.drop_table("outbox_event")
    op.drop_table("audit_event")
    op.drop_table("job")
    op.drop_table("snapshot")
    op.drop_table("comment")
    op.drop_table("annotation")
    op.drop_table("task")
    op.drop_table("item")
    op.drop_table("membership")
    op.drop_constraint("fk_project_label_schema_id", "project", type_="foreignkey")
    op.drop_table("label_schema_version")
    op.drop_table("label_schema")
    op.drop_table("project")
    op.drop_table("model_version")
    op.drop_table("model")
    op.drop_table("connector")
    op.drop_table("user")
    op.drop_table("organization")

    for enum_name in (
        "job_status",
        "job_type",
        "annotation_status",
        "annotation_source",
        "task_status",
        "task_type",
        "item_status",
        "media_type",
        "project_role",
        "model_task",
        "connector_identity",
        "connector_type",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
