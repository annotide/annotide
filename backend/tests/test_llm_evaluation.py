"""LLM evaluation items (§5 LLM-data): schema, results, QA-6, scan, export, agreement."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.exporters import ExportItem, get_exporter
from app.exporters.llm import llm_records
from app.schemas.annotation import (
    AnnotationResult,
    RankingShape,
    RatingShape,
    validate_against_schema,
)
from app.schemas.item import MediaType
from app.schemas.label_schema import LabelSchemaDefinition
from app.services.agreement import compute_item_agreement
from app.services.fusion import fuse
from app.services.scanning import media_type_for

SCHEMA = LabelSchemaDefinition.model_validate(
    {
        "version": 1,
        "classes": [
            {
                "name": "preference",
                "display_name": "Preference",
                "color": "#2563eb",
                "tools": ["ranking"],
                "attributes": [{"name": "why", "type": "text"}],
            },
            {
                "name": "helpfulness",
                "display_name": "Helpfulness",
                "color": "#16a34a",
                "tools": ["rating"],
                "scale": {"min": 1, "max": 5, "labels": {"1": "Useless", "5": "Excellent"}},
            },
        ],
        "classification": [{"name": "safe", "type": "boolean"}],
    }
)

DOCUMENT = {
    "messages": [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Capital of Finland?"},
    ],
    "responses": [
        {"id": "a", "content": "Helsinki.", "model": "m1"},
        {"id": "b", "content": "Tampere."},
        {"id": "c", "content": "I don't know."},
    ],
}


def _ranking(order: list[str], cls: str = "preference", **extra: Any) -> dict[str, Any]:
    return {"id": str(uuid4()), "type": "ranking", "class": cls, "order": order, **extra}


def _rating(target: str, value: int, cls: str = "helpfulness") -> dict[str, Any]:
    return {"id": str(uuid4()), "type": "rating", "class": cls, "target": target, "value": value}


def _result(*shapes: dict[str, Any], **classification: Any) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "llm",
            "classification": classification,
            "shapes": list(shapes),
        }
    )


class TestSchema:
    def test_a_rating_class_needs_a_scale_and_only_it_has_one(self) -> None:
        base = {"name": "x", "display_name": "X", "color": "#000000"}
        with pytest.raises(ValidationError, match="requires a scale"):
            LabelSchemaDefinition.model_validate(
                {"version": 1, "classes": [{**base, "tools": ["rating"]}]}
            )
        with pytest.raises(ValidationError, match="scale requires the rating tool"):
            LabelSchemaDefinition.model_validate(
                {
                    "version": 1,
                    "classes": [{**base, "tools": ["ranking"], "scale": {"min": 1, "max": 5}}],
                }
            )

    @pytest.mark.parametrize(
        ("scale", "message"),
        [
            ({"min": 5, "max": 5}, "below max"),
            ({"min": 0, "max": 21}, "at most 21 steps"),
            ({"min": 1, "max": 5, "labels": {"x": "?"}}, "not an integer"),
            ({"min": 1, "max": 5, "labels": {"9": "?"}}, "outside 1-5"),
        ],
    )
    def test_bad_scales(self, scale: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            LabelSchemaDefinition.model_validate(
                {
                    "version": 1,
                    "classes": [
                        {
                            "name": "x",
                            "display_name": "X",
                            "color": "#000000",
                            "tools": ["rating"],
                            "scale": scale,
                        }
                    ],
                }
            )


class TestResult:
    def test_ranking_and_rating_shapes(self) -> None:
        result = _result(
            _ranking(["a", "c", "b"], attributes={"why": "a is right"}),
            _rating("response:a", 5),
            _rating("message:1", 3),
            _rating("conversation", 4),
            safe=True,
        )
        assert validate_against_schema(result, SCHEMA) == []

    @pytest.mark.parametrize(
        ("shape", "message"),
        [
            (_ranking([]), "at least 1"),
            (_ranking(["a", "a"]), "each response once"),
            (_rating("turn:1", 3), "String should match pattern"),
            (_rating("message:01", 3), "String should match pattern"),
        ],
    )
    def test_malformed_shapes(self, shape: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            _result(shape)

    def test_llm_shapes_only_on_llm_items_and_nothing_else_there(self) -> None:
        with pytest.raises(ValidationError, match="only allowed on llm items"):
            AnnotationResult.model_validate(
                {"schema_version": 1, "media_type": "text", "shapes": [_ranking(["a"])]}
            )
        with pytest.raises(ValidationError, match="only ranking and rating"):
            _result(
                {"id": str(uuid4()), "type": "bbox", "class": "preference", "bbox": [0, 0, 1, 1]}
            )

    def test_qa6_checks_scale_tool_and_uniqueness(self) -> None:
        result = _result(
            _rating("response:a", 9),
            _rating("response:b", 2),
            _rating("response:b", 3),
            _ranking(["a", "b"]),
            _ranking(["b", "a"]),
            _ranking(["a"], cls="helpfulness"),
        )
        violations = validate_against_schema(result, SCHEMA)
        assert any("rating 9 is outside 1-5" in v for v in violations)
        assert any("a second 'helpfulness' rating for response:b" in v for v in violations)
        assert any("a second ranking for 'preference'" in v for v in violations)
        assert any("tool 'ranking' is not allowed for class 'helpfulness'" in v for v in violations)


def test_scan_maps_llm_json_before_json() -> None:
    # The scanner speaks the ORM enum; compare by value.
    assert media_type_for("evals/run-1/case-7.llm.json") == "llm"
    assert media_type_for("evals/CASE.LLM.JSON") == "llm"
    assert media_type_for("notes/data.json") == "text"


def _item(result: AnnotationResult, text: str | None, path: str = "case.llm.json") -> ExportItem:
    return ExportItem(id=uuid4(), path=path, width=None, height=None, result=result, text=text)


class TestExport:
    def test_records_and_preference_pairs(self) -> None:
        result = _result(
            _ranking(["a", "c", "b"]),
            _rating("response:a", 5),
            _rating("message:1", 4),
            safe=True,
        )
        [record], warnings = llm_records([_item(result, json.dumps(DOCUMENT))])

        assert warnings == []
        assert record["rankings"] == {"preference": ["a", "c", "b"]}
        assert [(r["target"], r["value"]) for r in record["ratings"]] == [
            ("response:a", 5),
            ("message:1", 4),
        ]
        assert record["classification"] == {"safe": True}
        pairs = [(p["chosen"]["id"], p["rejected"]["id"]) for p in record["preference_pairs"]]
        assert pairs == [("a", "c"), ("a", "b"), ("c", "b")]
        assert record["preference_pairs"][0]["prompt"] == DOCUMENT["messages"]
        assert record["preference_pairs"][0]["chosen"]["model"] == "m1"

    def test_dangling_ids_unreadable_documents_and_other_media_are_warned(self) -> None:
        dangling = _result(_ranking(["a", "zz"]), _rating("message:9", 3), _rating("response:q", 2))
        image = AnnotationResult.model_validate({"schema_version": 1, "media_type": "image"})
        records, warnings = llm_records(
            [
                _item(dangling, json.dumps(DOCUMENT), "d.llm.json"),
                _item(_result(), "not json", "broken.llm.json"),
                _item(_result(), None, "missing.llm.json"),
                _item(image, None, "cat.jpg"),
            ]
        )
        assert len(records) == 1
        assert records[0]["rankings"] == {"preference": ["a"]}
        assert records[0]["ratings"] == []
        assert records[0]["preference_pairs"] == []
        assert warnings == [
            "d.llm.json: ranking 'preference' names unknown responses ['zz']",
            "d.llm.json: rating 'helpfulness' targets unknown message:9",
            "d.llm.json: rating 'helpfulness' targets unknown response:q",
            "broken.llm.json: skipped, the LLM document could not be read",
            "missing.llm.json: skipped, the LLM document could not be read",
            "cat.jpg: skipped image item, not supported by this format",
        ]

    def test_the_exporter_writes_jsonl_and_warnings(self) -> None:
        files = list(
            get_exporter("llm").export(
                [
                    _item(_result(_ranking(["b", "a"])), json.dumps(DOCUMENT)),
                    _item(_result(), None, "gone.llm.json"),
                ],
                SCHEMA,
            )
        )
        by_path = {f.path: f.data for f in files}
        [line] = by_path["annotations.jsonl"].decode().splitlines()
        assert json.loads(line)["rankings"] == {"preference": ["b", "a"]}
        assert json.loads(by_path["warnings.json"]) == [
            "gone.llm.json: skipped, the LLM document could not be read"
        ]

    def test_image_formats_skip_llm_items(self) -> None:
        files = list(get_exporter("coco").export([_item(_result(), None)], SCHEMA))
        warnings = json.loads(next(f.data for f in files if f.path == "warnings.json"))
        assert warnings == ["case.llm.json: skipped llm item, not supported by this format"]


def _users(n: int) -> list[UUID]:
    return [uuid4() for _ in range(n)]


def test_ratings_and_rankings_count_in_agreement() -> None:
    a, b = _users(2)
    agreement = compute_item_agreement(
        {
            a: _result(_ranking(["a", "b"]), _rating("response:a", 5), _rating("response:b", 2)),
            b: _result(_ranking(["a", "b"]), _rating("response:a", 5), _rating("response:b", 3)),
        }
    )
    fields = {row.field: row.items for row in agreement.classification}
    assert fields == {"ranking:preference": 1, "rating:helpfulness": 2}
    [pair] = agreement.pairs
    assert pair.cohen_kappa is not None


def test_fusion_votes_rankings_and_ratings() -> None:
    results = [
        _result(_ranking(["a", "b"]), _rating("response:a", 5), _rating("response:b", 1)),
        _result(_ranking(["a", "b"]), _rating("response:a", 5), _rating("response:b", 2)),
        _result(_ranking(["b", "a"]), _rating("response:a", 4), _rating("response:b", 3)),
    ]
    fused, conflicts = fuse(results)

    rankings = [s for s in fused.shapes if isinstance(s, RankingShape)]
    ratings = {s.target: s.value for s in fused.shapes if isinstance(s, RatingShape)}
    assert [r.order for r in rankings] == [["a", "b"]]
    assert ratings == {"response:a": 5}
    assert conflicts == ["rating.helpfulness.response:b"]
    assert fused.media_type is MediaType.LLM
