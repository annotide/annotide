"""`project.cache_connector_id` and the `rebuild_cache` job type (SRC-6).

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is committed immediately; see 0003.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'rebuild_cache'")

    op.add_column(
        "project",
        sa.Column("cache_connector_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "project_cache_connector_id_fkey",
        "project",
        "connector",
        ["cache_connector_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_project_cache_connector_id", "project", ["cache_connector_id"])


def downgrade() -> None:
    op.drop_index("ix_project_cache_connector_id", table_name="project")
    op.drop_constraint("project_cache_connector_id_fkey", "project", type_="foreignkey")
    op.drop_column("project", "cache_connector_id")
    # PostgreSQL cannot drop a single enum value; `rebuild_cache` stays.
