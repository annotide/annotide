"""Folder-level access: membership.path_prefixes (§4).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "membership",
        sa.Column("path_prefixes", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("membership", "path_prefixes")
