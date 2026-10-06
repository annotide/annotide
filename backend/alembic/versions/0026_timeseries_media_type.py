"""`timeseries` media type: CSV channels over a time axis (§5).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE media_type ADD VALUE IF NOT EXISTS 'timeseries'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; an unused `timeseries` is harmless.
    pass
