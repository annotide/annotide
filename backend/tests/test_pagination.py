"""Tests for opaque cursor pagination."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.services.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cursor,
    InvalidCursorError,
    build_page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


@dataclass(frozen=True)
class Row:
    id: UUID
    created_at: datetime


def make_rows(n: int) -> list[Row]:
    base = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    return [Row(id=uuid4(), created_at=base.replace(minute=i)) for i in range(n)]


class TestRoundTrip:
    def test_simple_key(self) -> None:
        cursor = Cursor(key=(42, "abc"), direction="asc")
        assert decode_cursor(encode_cursor(cursor)) == cursor

    def test_uuid_survives(self) -> None:
        value = uuid4()
        decoded = decode_cursor(encode_cursor(Cursor(key=(value,))))
        assert decoded.key == (value,)
        assert isinstance(decoded.key[0], UUID)

    def test_datetime_survives_with_timezone(self) -> None:
        value = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)
        decoded = decode_cursor(encode_cursor(Cursor(key=(value,))))
        assert decoded.key == (value,)
        assert decoded.key[0].tzinfo is not None

    def test_token_is_url_safe_and_unpadded(self) -> None:
        token = encode_cursor(Cursor(key=(uuid4(), datetime.now(UTC))))
        assert "=" not in token
        assert "+" not in token
        assert "/" not in token


class TestRejects:
    @pytest.mark.parametrize(
        "token",
        ["", "!!!!", "bm90LWpzb24", "e30", "eyJkIjoiYXNjIn0"],
        ids=["empty", "not-base64", "not-json", "no-key", "missing-key"],
    )
    def test_malformed_tokens(self, token: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(token)

    def test_bad_direction(self) -> None:
        # Hand-build a token the encoder would never produce.
        raw = json.dumps({"k": [1], "d": "sideways"}).encode()
        tampered = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with pytest.raises(InvalidCursorError):
            decode_cursor(tampered)

    def test_key_must_be_a_list(self) -> None:
        raw = json.dumps({"k": "not-a-list", "d": "desc"}).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with pytest.raises(InvalidCursorError):
            decode_cursor(token)


class TestClampLimit:
    def test_none_gives_the_default(self) -> None:
        assert clamp_limit(None) == DEFAULT_LIMIT

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, 1),
            (-5, 1),
            (10, 10),
            (MAX_LIMIT, MAX_LIMIT),
            (MAX_LIMIT + 1, MAX_LIMIT),
            (10_000, MAX_LIMIT),
        ],
    )
    def test_clamping(self, given: int, expected: int) -> None:
        assert clamp_limit(given) == expected


class TestBuildPage:
    def test_full_page_yields_a_cursor(self) -> None:
        rows = make_rows(11)
        page = build_page(rows, 10, lambda r: (r.created_at, r.id))
        assert len(page.items) == 10
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor).key == (rows[9].created_at, rows[9].id)

    def test_partial_page_has_no_cursor(self) -> None:
        page = build_page(make_rows(4), 10, lambda r: (r.id,))
        assert len(page.items) == 4
        assert page.next_cursor is None

    def test_exactly_limit_rows_has_no_cursor(self) -> None:
        # No over-fetched row came back, so there is nothing after this page.
        page = build_page(make_rows(10), 10, lambda r: (r.id,))
        assert len(page.items) == 10
        assert page.next_cursor is None

    def test_empty_result(self) -> None:
        page = build_page([], 10, lambda r: (r.id,))
        assert page.items == []
        assert page.next_cursor is None

    def test_direction_is_carried_into_the_cursor(self) -> None:
        rows = make_rows(11)
        page = build_page(rows, 10, lambda r: (r.id,), direction="asc")
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor).direction == "asc"
