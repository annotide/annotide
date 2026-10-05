"""Add `item.thumbnail_path` and the `thumbnail` job type (IMG-8, UX-6).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("item", sa.Column("thumbnail_path", sa.Text(), nullable=True))
    # Same shape as 0003: enum values are added outside the transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'thumbnail'")


def downgrade() -> None:
    op.drop_column("item", "thumbnail_path")
    # PostgreSQL cannot drop a single enum value; left in place on purpose.
