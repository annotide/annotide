"""Add `license_state.host_mismatch_since` for host binding (LIC-29).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("license_state", sa.Column("host_mismatch_since", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("license_state", "host_mismatch_since")
