"""Idempotency keys in their own table (API-2).

Moves the `Idempotency-Key` bookkeeping out of `project.settings` JSONB into
`idempotency_key` with a unique index, and lets job creates use it too.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_key",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(64), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "organization_id", "endpoint", "key", name="uq_idempotency_key_org_endpoint_key"
        ),
    )
    # Carry over the keys phase 1 stashed in `project.settings`, then drop them.
    op.execute(
        """
        INSERT INTO idempotency_key (organization_id, endpoint, key, target_id, created_at)
        SELECT organization_id, 'project.create', settings->>'_idempotency_key', id, created_at
        FROM project
        WHERE settings->>'_idempotency_key' IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute("UPDATE project SET settings = settings - '_idempotency_key'")


def downgrade() -> None:
    op.execute(
        """
        UPDATE project p
        SET settings = p.settings || jsonb_build_object('_idempotency_key', k.key)
        FROM idempotency_key k
        WHERE k.endpoint = 'project.create' AND k.target_id = p.id
        """
    )
    op.drop_table("idempotency_key")
