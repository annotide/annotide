"""Models without an endpoint: external producers that post pre-labels (API-8).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("model", "endpoint_url", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # An external producer has nothing to fall back to; give it a placeholder
    # that fails loudly if anything ever calls it.
    op.execute(
        "UPDATE model SET endpoint_url = 'http://external.invalid' WHERE endpoint_url IS NULL"
    )
    op.alter_column("model", "endpoint_url", existing_type=sa.Text(), nullable=False)
