"""TOTP MFA on `user` (AUTH-2).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column("user", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.add_column("user", sa.Column("mfa_recovery_codes", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for name in ("mfa_recovery_codes", "totp_last_step", "totp_enabled_at", "totp_secret"):
        op.drop_column("user", name)
