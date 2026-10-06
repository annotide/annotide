"""Tests for the pure parts of `app/services/comments.py` (WF-5 mentions)."""

from __future__ import annotations

from app.services.comments import extract_mentions


class TestExtractMentions:
    def test_finds_several_addresses_in_order(self) -> None:
        body = "Ping @anna@example.com and @bo.b+x@sub.example.org please"
        assert extract_mentions(body) == ["anna@example.com", "bo.b+x@sub.example.org"]

    def test_lower_cases_and_de_duplicates(self) -> None:
        body = "@Anna@Example.com again @anna@example.com"
        assert extract_mentions(body) == ["anna@example.com"]

    def test_trailing_punctuation_is_not_part_of_the_address(self) -> None:
        assert extract_mentions("thanks @anna@example.com.") == ["anna@example.com"]
        assert extract_mentions("(@anna@example.com)") == ["anna@example.com"]
        assert extract_mentions("@anna@example.com, @bob@example.com!") == [
            "anna@example.com",
            "bob@example.com",
        ]

    def test_bare_at_and_handles_are_not_mentions(self) -> None:
        assert extract_mentions("email me @ home") == []
        assert extract_mentions("@anna is not an address") == []
        assert extract_mentions("@anna@localhost has no dot") == []
        assert extract_mentions("") == []
