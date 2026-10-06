"""SCIM provisioning (AUTH-3): token, user SCIM columns, scim_group tables.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("scim_token_hash", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_organization_scim_token_hash", "organization", ["scim_token_hash"]
    )
    op.add_column("user", sa.Column("scim_external_id", sa.String(255), nullable=True))
    op.add_column("user", sa.Column("scim_deleted_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "scim_group",
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
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("organization_id", "display_name", name="uq_scim_group_name"),
    )
    op.create_index("ix_scim_group_organization_id", "scim_group", ["organization_id"])

    op.create_table(
        "scim_group_member",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scim_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("group_id", "user_id", name="uq_scim_group_member"),
    )
    op.create_index("ix_scim_group_member_group_id", "scim_group_member", ["group_id"])
    op.create_index("ix_scim_group_member_user_id", "scim_group_member", ["user_id"])


def downgrade() -> None:
    op.drop_table("scim_group_member")
    op.drop_table("scim_group")
    op.drop_column("user", "scim_deleted_at")
    op.drop_column("user", "scim_external_id")
    op.drop_constraint("uq_organization_scim_token_hash", "organization", type_="unique")
    op.drop_column("organization", "scim_token_hash")
