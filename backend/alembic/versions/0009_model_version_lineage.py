"""Add lineage columns to `model_version` (EXP-8).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_version",
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("snapshot.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "model_version", sa.Column("snapshot_digest", sa.String(length=64), nullable=True)
    )
    op.add_column("model_version", sa.Column("training_run", postgresql.JSONB(), nullable=True))
    op.create_index("ix_model_version_snapshot_id", "model_version", ["snapshot_id"])
    op.create_index("ix_model_version_snapshot_digest", "model_version", ["snapshot_digest"])


def downgrade() -> None:
    op.drop_index("ix_model_version_snapshot_digest", table_name="model_version")
    op.drop_index("ix_model_version_snapshot_id", table_name="model_version")
    op.drop_column("model_version", "training_run")
    op.drop_column("model_version", "snapshot_digest")
    op.drop_column("model_version", "snapshot_id")
