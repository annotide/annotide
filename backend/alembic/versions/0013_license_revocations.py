"""Add `license_state.revocations`, the latest verified revocation list (LIC-8).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("license_state", sa.Column("revocations", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("license_state", "revocations")
