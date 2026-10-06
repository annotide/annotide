"""Notification e-mail and chat webhook formats (API-7).

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

webhook_format = postgresql.ENUM("json", "slack", "teams", name="webhook_format", create_type=False)


def upgrade() -> None:
    webhook_format.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "webhook",
        sa.Column("format", webhook_format, nullable=False, server_default="json"),
    )
    op.add_column(
        "user",
        sa.Column("email_notifications", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "notification", sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "notification",
        sa.Column("email_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("notification", "email_attempts")
    op.drop_column("notification", "emailed_at")
    op.drop_column("user", "email_notifications")
    op.drop_column("webhook", "format")
    webhook_format.drop(op.get_bind(), checkfirst=True)
