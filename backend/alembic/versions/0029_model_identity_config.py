"""`model.identity_config`: the non-secret half of an Entra identity (BYOM-3).

Model endpoints can now sign in with a managed identity or a service
principal. The token scope, tenant and client ids live here; a service
principal's client secret stays behind `secret_ref`.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model",
        sa.Column(
            "identity_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("model", "identity_config")
