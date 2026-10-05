"""Add the `import` job type (EXP-6).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
    # PostgreSQL < 12 and is committed immediately on newer versions either
    # way, so it gets its own autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'import'")


def downgrade() -> None:
    # PostgreSQL cannot drop a single enum value; rows of this type would have
    # to be deleted and the type rebuilt. Left as a no-op on purpose.
    pass
