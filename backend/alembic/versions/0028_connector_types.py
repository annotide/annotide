"""Connector types `sharepoint` and `databricks_volume` (§3).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE connector_type ADD VALUE IF NOT EXISTS 'sharepoint'")
        op.execute("ALTER TYPE connector_type ADD VALUE IF NOT EXISTS 'databricks_volume'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; unused values are harmless.
    pass
