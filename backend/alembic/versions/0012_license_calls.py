"""Licence refresh and heartbeat state on `license_state` (LIC-6, LIC-27).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TIMESTAMPS = (
    "refresh_attempted_at",
    "refresh_succeeded_at",
    "heartbeat_attempted_at",
    "heartbeat_sent_at",
)
_TEXTS = ("last_host", "refresh_error", "heartbeat_error")
_PAYLOADS = ("refresh_payload", "heartbeat_payload")


def upgrade() -> None:
    for name in _TEXTS:
        op.add_column("license_state", sa.Column(name, sa.Text(), nullable=True))
    for name in _TIMESTAMPS:
        op.add_column("license_state", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in _PAYLOADS:
        op.add_column("license_state", sa.Column(name, postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for name in (*_PAYLOADS, *_TIMESTAMPS, *_TEXTS):
        op.drop_column("license_state", name)
