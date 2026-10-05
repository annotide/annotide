"""Capitalisation-rule NER of the reference backend, and `/predict` on text items."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.backends.text_ner import find_entities
from app.main import app


def _labelled(text: str) -> list[tuple[str, str]]:
    return [(text[e.start : e.end], e.label) for e in find_entities(text)]


class TestFindEntities:
    def test_rules(self) -> None:
        text = (
            "Yesterday Anna Virtanen met the board of Nokia Oyj in Espoo. "
            "She works for Contoso with Bob."
        )
        assert _labelled(text) == [
            ("Anna Virtanen", "PER"),
            ("Nokia Oyj", "ORG"),
            ("Espoo", "LOC"),
            ("Contoso", "ORG"),
            ("Bob", "PER"),
        ]

    def test_single_word_at_sentence_start_is_not_an_entity(self) -> None:
        assert _labelled("Yesterday it rained. Today too.") == []

    def test_offsets_are_code_points(self) -> None:
        text = "😀 then Maria Lopez arrived"
        (entity,) = find_entities(text)
        assert text[entity.start : entity.end] == "Maria Lopez"
        assert entity.start == 7  # the emoji is one code point

    def test_trailing_inc_keeps_its_dot(self) -> None:
        assert _labelled("We sold it to Acme Inc. last year") == [("Acme Inc.", "ORG")]


SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {"name": "PER", "display_name": "Person", "color": "#e11d48", "tools": ["span"]},
        {"name": "ORG", "display_name": "Org", "color": "#2563eb", "tools": ["span"]},
    ],
}


@pytest.fixture
def client() -> Any:
    with TestClient(app) as test_client:
        yield test_client


def test_predict_on_a_text_item_returns_spans_of_schema_classes(client: Any) -> None:
    text = "The report by Anna Virtanen reached Nokia Oyj in Espoo."
    url = "data:text/plain;base64," + base64.b64encode(text.encode()).decode()
    response = client.post(
        "/predict",
        json={
            "items": [{"id": "t1", "url": url, "width": 0, "height": 0, "media_type": "text"}],
            "schema": SCHEMA,
        },
    )
    assert response.status_code == 200, response.text
    (prediction,) = response.json()["predictions"]
    assert prediction["error"] is None
    result = prediction["result"]
    assert result["media_type"] == "text"
    # LOC is not in the schema, so "Espoo" is left out.
    assert [(s["class"], s["text"]) for s in result["shapes"]] == [
        ("PER", "Anna Virtanen"),
        ("ORG", "Nokia Oyj"),
    ]
    assert all(s["type"] == "span" and 0 < s["confidence"] <= 1 for s in result["shapes"])


def _labelled(text: str) -> list[tuple[str, str]]:
    return [(text[e.start : e.end], e.label) for e in find_entities(text)]


def test_a_line_start_is_a_sentence_start() -> None:
    # One field per line: the first word of a line is capitalised by layout.
    assert _labelled("Total 1240,00\nDue soon\nAnna Virtanen") == [("Anna Virtanen", "PER")]


def test_labels_dates_and_codes_are_not_names() -> None:
    assert _labelled("Paid in EUR by Attn: Mikko on Friday, 5 September.") == [("Mikko", "PER")]


def test_place_after_a_comma_or_a_postcode() -> None:
    assert _labelled("Bill to Contoso Ltd, Tampere and 00100 Helsinki") == [
        ("Contoso Ltd", "ORG"),
        ("Tampere", "LOC"),
        ("Helsinki", "LOC"),
    ]


def test_a_list_shares_its_label_and_an_org_stays_an_org() -> None:
    assert _labelled("We met in Turku and Oulu with Contoso Ltd. Write to Contoso.") == [
        ("Turku", "LOC"),
        ("Oulu", "LOC"),
        ("Contoso Ltd.", "ORG"),
        ("Contoso", "ORG"),
    ]
