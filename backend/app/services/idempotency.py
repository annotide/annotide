"""`Idempotency-Key` bookkeeping for creation endpoints (API-2).

A create that carries the header first asks :func:`find` for an earlier
result; if there is none it creates its row and calls :func:`remember` in
the same transaction. Two concurrent retries both miss the lookup, but only
one can commit the unique `(organization_id, endpoint, key)` row: the loser
gets :class:`sqlalchemy.exc.IntegrityError` on commit, rolls back and asks
:func:`find` again. Keys are scoped to the organisation, not the user, so a
retry from a second session of the same client is still a replay.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey


async def find(
    session: AsyncSession, *, organization_id: UUID, endpoint: str, key: str
) -> UUID | None:
    """The id created by an earlier request with this key, if any."""
    target_id: UUID | None = await session.scalar(
        select(IdempotencyKey.target_id).where(
            IdempotencyKey.organization_id == organization_id,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.key == key,
        )
    )
    return target_id


def remember(
    session: AsyncSession, *, organization_id: UUID, endpoint: str, key: str, target_id: UUID
) -> None:
    """Add the key row to the caller's transaction; the caller commits."""
    session.add(
        IdempotencyKey(
            organization_id=organization_id, endpoint=endpoint, key=key, target_id=target_id
        )
    )
