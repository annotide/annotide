"""`connector.event_token_hash` for event-driven discovery (SRC-3).

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("connector", sa.Column("event_token_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("connector", "event_token_hash")
