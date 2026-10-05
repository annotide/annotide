"""Add `snapshot.split` — the train / val / test partition config (EXP-3).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("snapshot", sa.Column("split", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("snapshot", "split")
