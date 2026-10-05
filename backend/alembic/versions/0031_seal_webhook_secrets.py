"""Seal `webhook.secret` at rest (API-4, SEC-5).

Signing secrets were stored in clear. They are now AES-GCM sealed under
`APP_SECRET_KEY` (`services.webhooks.seal_secret`), so a database dump alone
cannot forge deliveries. Data only: clear secrets are 64 hex characters and
a sealed one (124 characters of url-safe base64) never is, so a re-run is a
no-op. Needs `APP_SECRET_KEY` only when there is something to seal.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-02
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.security import seal, unseal

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `services.webhooks._SEAL_PURPOSE`; a migration must not import services.
_PURPOSE = "webhook-secret"
_CLEAR = re.compile(r"[0-9a-f]{64}")

# Untyped id: the raw value read back is the value written, whatever the dialect.
_webhook = sa.table("webhook", sa.column("id"), sa.column("secret", sa.String()))


def upgrade() -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.select(_webhook.c.id, _webhook.c.secret)).all():
        if _CLEAR.fullmatch(row.secret):
            bind.execute(
                _webhook.update()
                .where(_webhook.c.id == row.id)
                .values(secret=seal(row.secret, purpose=_PURPOSE))
            )


def downgrade() -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.select(_webhook.c.id, _webhook.c.secret)).all():
        if not _CLEAR.fullmatch(row.secret):
            bind.execute(
                _webhook.update()
                .where(_webhook.c.id == row.id)
                .values(secret=unseal(row.secret, purpose=_PURPOSE))
            )
