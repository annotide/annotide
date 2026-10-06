"""Parallel annotate tasks and annotation kinds (QA-1, QA-4, IMG-6).

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

annotation_kind = postgresql.ENUM(
    "primary", "consensus", "gold", name="annotation_kind", create_type=False
)


def upgrade() -> None:
    annotation_kind.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "annotation",
        sa.Column("kind", annotation_kind, nullable=False, server_default="primary"),
    )
    op.create_index("ix_annotation_item_id_kind", "annotation", ["item_id", "kind"])
    op.add_column("task", sa.Column("slot", sa.Integer(), nullable=True))
    op.add_column("task", sa.Column("region", postgresql.JSONB(), nullable=True))
    op.add_column(
        "task", sa.Column("gold", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    for name in ("gold", "region", "slot"):
        op.drop_column("task", name)
    op.drop_index("ix_annotation_item_id_kind", table_name="annotation")
    op.drop_column("annotation", "kind")
    annotation_kind.drop(op.get_bind(), checkfirst=True)
