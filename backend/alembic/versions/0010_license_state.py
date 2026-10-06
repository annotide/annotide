"""Add `license_state`: stored licence key and clock high-water mark (LIC-25, LIC-26).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "license_state",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slot", sa.String(16), nullable=False, server_default="install"),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("key_source", sa.String(16), nullable=True),
        sa.Column("clock_high_water", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("slot", name="uq_license_state_slot"),
    )
    # The one row exists from the start, so sign-ins never race to create it.
    op.execute("INSERT INTO license_state (slot) VALUES ('install')")


def downgrade() -> None:
    op.drop_table("license_state")
