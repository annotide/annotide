"""Soft-deleted models: `model.deleted_at`.

Deleting a model used to remove its versions, which set the author of every
pre-label they wrote to null and broke `ck_annotation_author_xor` (a 500).
A soft-deleted model keeps its rows; the API treats it as gone.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("model", "deleted_at")
