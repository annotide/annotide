"""Opaque cursor pagination (API-2).

Offset pagination drifts when rows are inserted mid-scroll, which matters here:
the item list is the annotator's work queue and new items arrive from source
scans while people are looking at it. So cursors encode the sort key of the last
row seen, and the next page asks for rows strictly after it.

The cursor is base64url of ``{"k": [...], "d": "asc"|"desc"}``. It is opaque to
clients by contract — never parse it in the frontend.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

Direction = Literal["asc", "desc"]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class InvalidCursorError(ValueError):
    """The cursor was not produced by :func:`encode_cursor`."""


@dataclass(frozen=True, slots=True)
class Cursor:
    """The position of the last row of the previous page."""

    key: tuple[Any, ...]
    direction: Direction = "desc"


def _encode_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "__uuid__" in value:
            return UUID(value["__uuid__"])
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
    return value


def encode_cursor(cursor: Cursor) -> str:
    """Serialise a cursor to a URL-safe token."""
    payload = {"k": [_encode_value(v) for v in cursor.key], "d": cursor.direction}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor:
    """Parse a cursor token, raising :class:`InvalidCursorError` on anything else."""
    padded = token + "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("cursor is not a valid token") from exc

    if not isinstance(payload, dict) or "k" not in payload:
        raise InvalidCursorError("cursor is missing its sort key")

    direction = payload.get("d", "desc")
    if direction not in ("asc", "desc"):
        raise InvalidCursorError("cursor direction must be 'asc' or 'desc'")

    key = payload["k"]
    if not isinstance(key, list):
        raise InvalidCursorError("cursor sort key must be a list")

    return Cursor(key=tuple(_decode_value(v) for v in key), direction=direction)


def clamp_limit(limit: int | None) -> int:
    """Keep page sizes inside the range the API promises."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


@dataclass(frozen=True, slots=True)
class PageResult[TRow]:
    """A page plus the cursor that fetches the next one."""

    items: list[TRow]
    next_cursor: str | None


def build_page[TRow](
    rows: list[TRow],
    limit: int,
    key_of: Callable[[TRow], tuple[Any, ...]],
    direction: Direction = "desc",
) -> PageResult[TRow]:
    """Trim an over-fetched row list into a page and mint the next cursor.

    Callers fetch ``limit + 1`` rows. If the extra row came back there is another
    page, and the cursor points at the last row we actually return.

    ``key_of`` extracts the sort-key tuple from a row; it is a callable so this
    module stays free of ORM imports.
    """
    has_more = len(rows) > limit
    page = rows[:limit]
    if not has_more or not page:
        return PageResult(items=page, next_cursor=None)

    cursor = Cursor(key=key_of(page[-1]), direction=direction)
    return PageResult(items=page, next_cursor=encode_cursor(cursor))
