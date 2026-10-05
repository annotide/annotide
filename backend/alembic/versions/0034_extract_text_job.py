"""The `extract_text` job type (PDF text mode).

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is committed immediately; see 0003.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'extract_text'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value; `extract_text` stays.
    pass
