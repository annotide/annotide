"""`model_version.parent_version_id` and `derivation` (EXP-8).

A version can name the version it was trained, distilled or quantized from,
so the UI can draw a model's family: teacher → student → int8.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_version",
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_version.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("model_version", sa.Column("derivation", sa.String(length=16), nullable=True))
    op.create_index("ix_model_version_parent_version_id", "model_version", ["parent_version_id"])


def downgrade() -> None:
    op.drop_index("ix_model_version_parent_version_id", table_name="model_version")
    op.drop_column("model_version", "derivation")
    op.drop_column("model_version", "parent_version_id")
