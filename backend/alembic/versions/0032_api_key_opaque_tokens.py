"""Opaque API key tokens (AUTH-4): `api_key.token_prefix`, `api_key.token_hash`.

New keys are `ant_<prefix>_<secret>`: the row keeps the prefix (unique, to
find it) and a sha256 of the token, so a key survives an `APP_SECRET_KEY`
rotation and cannot be read back from the database. Existing rows are JWT
keys; both columns stay null for them and they keep working until revoked
or expired.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_key", sa.Column("token_prefix", sa.String(length=16), nullable=True))
    op.add_column("api_key", sa.Column("token_hash", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_api_key_token_prefix", "api_key", ["token_prefix"])


def downgrade() -> None:
    op.drop_constraint("uq_api_key_token_prefix", "api_key", type_="unique")
    op.drop_column("api_key", "token_hash")
    op.drop_column("api_key", "token_prefix")
