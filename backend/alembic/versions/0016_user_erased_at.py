"""`user.erased_at` for GDPR erasure (SEC-6).

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "erased_at")
