"""When a failed licence call may run again (LIC-6, LIC-27).

A cold licence server is retried after minutes, anything else hourly; the
count of failures in a row decides when quick retries stop.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CALLS = ("refresh", "heartbeat")


def upgrade() -> None:
    for call in _CALLS:
        op.add_column(
            "license_state",
            sa.Column(f"{call}_retry_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "license_state",
            sa.Column(f"{call}_failures", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for call in _CALLS:
        op.drop_column("license_state", f"{call}_failures")
        op.drop_column("license_state", f"{call}_retry_at")
