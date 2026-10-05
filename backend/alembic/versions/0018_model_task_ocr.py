"""Add the `ocr` model task (reading scanned PDFs).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is committed immediately; see 0003.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE model_task ADD VALUE IF NOT EXISTS 'ocr'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value. Left as a no-op on purpose.
    pass
