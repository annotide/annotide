"""Shared query helpers so routers stay thin.

Two things every project-scoped endpoint needs — keyset pagination and an
authorisation check — live here rather than being re-implemented per router,
because both are easy to get subtly wrong in ways that are hard to see in a
code review.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.api.errors import ForbiddenError, NotFoundError
from app.services.pagination import (
    Cursor,
    Direction,
    PageResult,
    build_page,
    decode_cursor,
)


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    limit: int,
    cursor: str | None,
    order_by: Sequence[InstrumentedAttribute[Any]],
    key_of: Callable[[Any], tuple[Any, ...]],
    direction: Direction = "desc",
) -> PageResult[Any]:
    """Run a keyset-paginated query.

    ``order_by`` must be the columns that make a row unique in the sort order —
    typically ``(created_at, id)``. The trailing ``id`` matters: without a
    tiebreaker, rows sharing a timestamp can be skipped or repeated between
    pages.

    The cursor becomes a row-value comparison (``(a, b) < (:a, :b)``) rather
    than an ``OFFSET``, so inserts during scrolling do not shift the window.
    """
    if not order_by:
        raise ValueError("paginate requires at least one order_by column")

    if cursor:
        position = decode_cursor(cursor)
        if len(position.key) != len(order_by):
            # A cursor minted for a different sort order would silently
            # mis-compare, so refuse it instead.
            raise ValueError("cursor does not match this query's sort order")
        row = tuple_(*order_by)
        stmt = stmt.where(row < position.key if direction == "desc" else row > position.key)

    ordered = [column.desc() if direction == "desc" else column.asc() for column in order_by]
    # Over-fetch one row: its presence is how we know another page exists.
    stmt = stmt.order_by(*ordered).limit(limit + 1)

    rows = list(await session.scalars(stmt))
    return build_page(rows, limit, key_of, direction=direction)


async def get_or_404[TModel](
    session: AsyncSession,
    model: type[TModel],
    entity_id: UUID,
    *,
    organization_id: UUID | None = None,
) -> TModel:
    """Fetch by primary key or raise :class:`NotFoundError`.

    A row belonging to another organisation raises ``NotFoundError``, never
    ``ForbiddenError``: a 403 would confirm the id exists, letting a caller
    enumerate other tenants' identifiers.
    """
    entity = await session.get(model, entity_id)
    # A soft-deleted row (`deleted_at` set) is gone as far as callers know.
    if entity is None or getattr(entity, "deleted_at", None) is not None:
        raise NotFoundError(f"{model.__name__} {entity_id} does not exist.")

    if organization_id is not None:
        owner = getattr(entity, "organization_id", None)
        if owner is not None and owner != organization_id:
            raise NotFoundError(f"{model.__name__} {entity_id} does not exist.")

    return entity


async def ensure_project_member(
    session: AsyncSession,
    project_id: UUID,
    user: Any,
) -> str:
    """Return the caller's role in a project, or refuse.

    The single authorisation entry point for project-scoped endpoints. Raises
    ``NotFoundError`` when the project is outside the caller's organisation (see
    :func:`get_or_404` for why) and ``ForbiddenError`` when they can see the
    organisation but are not a member of the project.
    """
    # Imported here: app.models pulls in the whole ORM, and the service layer is
    # imported by modules that must stay light.
    from sqlalchemy import select

    from app.models import Membership, Project

    project = await get_or_404(session, Project, project_id, organization_id=user.organization_id)

    if getattr(user, "is_superuser", False):
        return "owner"

    role = await session.scalar(
        select(Membership.role).where(
            Membership.project_id == project.id,
            Membership.user_id == user.id,
        )
    )
    if role is None:
        raise ForbiddenError("You are not a member of this project.")

    return str(getattr(role, "value", role))


async def member_prefixes(session: AsyncSession, project_id: UUID, user: Any) -> list[str] | None:
    """The caller's folder limits in a project, or None for the whole project.

    Superusers and owners are never limited. Call after `ensure_project_member`.
    """
    if getattr(user, "is_superuser", False):
        return None
    from sqlalchemy import select

    from app.models import Membership

    row = (
        await session.execute(
            select(Membership.role, Membership.path_prefixes).where(
                Membership.project_id == project_id, Membership.user_id == user.id
            )
        )
    ).first()
    if row is None:
        return None
    role, prefixes = row
    if str(getattr(role, "value", role)) == "owner" or not prefixes:
        return None
    return [str(prefix) for prefix in prefixes]


def path_in_scope(path: str, prefixes: list[str] | None) -> bool:
    return prefixes is None or any(path.startswith(prefix) for prefix in prefixes)


def path_scope_clause(column: Any, prefixes: list[str]) -> Any:
    """SQL: `column` starts with one of `prefixes` (LIKE-safe)."""
    from sqlalchemy import or_

    return or_(*(column.startswith(prefix, autoescape=True) for prefix in prefixes))


async def ensure_item_member(session: AsyncSession, item: Any, user: Any) -> str:
    """`ensure_project_member` for one item, honouring folder-level limits.

    An item outside the caller's folders is 404, exactly as if it did not
    exist: a limited member has no business learning its path or id.
    """
    role = await ensure_project_member(session, item.project_id, user)
    prefixes = await member_prefixes(session, item.project_id, user)
    if not path_in_scope(item.path, prefixes):
        raise NotFoundError(f"Item {item.id} does not exist.")
    return role


def cursor_for(*values: Any, direction: Direction = "desc") -> Cursor:
    """Build a cursor from an explicit sort key. Mostly a testing convenience."""
    return Cursor(key=tuple(values), direction=direction)
