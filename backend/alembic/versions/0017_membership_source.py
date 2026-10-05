"""`membership.source`: manual or IdP group sync (AUTH-3).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

membership_source = postgresql.ENUM("manual", "idp", name="membership_source", create_type=False)


def upgrade() -> None:
    membership_source.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "membership",
        sa.Column("source", membership_source, nullable=False, server_default="manual"),
    )


def downgrade() -> None:
    op.drop_column("membership", "source")
    membership_source.drop(op.get_bind(), checkfirst=True)
