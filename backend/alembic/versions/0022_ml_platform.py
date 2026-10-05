"""ML platforms (API-6): MLflow, Databricks and Azure ML workspaces.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

ml_platform_kind = postgresql.ENUM(
    "mlflow", "databricks", "azureml", name="ml_platform_kind", create_type=False
)


def upgrade() -> None:
    ml_platform_kind.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "ml_platform",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", ml_platform_kind, nullable=False),
        sa.Column("tracking_uri", sa.Text(), nullable=False),
        sa.Column("identity_type", sa.String(32), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_ml_platform_organization_name"),
    )
    op.create_index("ix_ml_platform_organization_id", "ml_platform", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_ml_platform_organization_id", table_name="ml_platform")
    op.drop_table("ml_platform")
    ml_platform_kind.drop(op.get_bind(), checkfirst=True)
