"""`llm` media type: LLM evaluation items (§5 LLM-data).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # A new enum value cannot be used in the transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE media_type ADD VALUE IF NOT EXISTS 'llm'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; an unused `llm` is harmless.
    pass
