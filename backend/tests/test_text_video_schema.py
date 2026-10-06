"""Tests for text (span/relation) and video annotation-result validation (TOOL, EXP-6, QA-6)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.annotation import AnnotationResult, validate_against_schema
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, ToolType

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


def _text_definition() -> LabelSchemaDefinition:
    return LabelSchemaDefinition(
        version=1,
        classes=[
            ClassDef(
                name="PER",
                display_name="Person",
                color="#e11d48",
                tools=[ToolType.SPAN],
            ),
            ClassDef(
                name="works_for",
                display_name="Works for",
                color="#22c55e",
                tools=[ToolType.RELATION],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# media-type rules on shapes
# ---------------------------------------------------------------------------


def test_video_shape_requires_frame() -> None:
    with pytest.raises(ValidationError, match="frame"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "video",
                "shapes": [{"id": _uid(), "type": "bbox", "class": "car", "bbox": [0, 0, 1, 1]}],
            }
        )


def test_video_shape_accepts_frame() -> None:
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "video",
            "shapes": [
                {
                    "id": _uid(),
                    "type": "bbox",
                    "class": "car",
                    "bbox": [0, 0, 1, 1],
                    "frame": 3,
                    "track_id": _uid(),
                }
            ],
        }
    )
    assert result.shapes[0].frame == 3


def test_non_video_shape_forbids_frame() -> None:
    with pytest.raises(ValidationError, match="frame"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "image",
                "shapes": [
                    {
                        "id": _uid(),
                        "type": "bbox",
                        "class": "car",
                        "bbox": [0, 0, 1, 1],
                        "frame": 0,
                    }
                ],
            }
        )


def test_span_only_allowed_on_text_items() -> None:
    with pytest.raises(ValidationError, match="text"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "image",
                "shapes": [{"id": _uid(), "type": "span", "class": "PER", "start": 0, "end": 5}],
            }
        )


def test_relation_only_allowed_on_text_items() -> None:
    a, b = _uid(), _uid()
    with pytest.raises(ValidationError, match="text"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "image",
                "shapes": [{"id": a, "type": "relation", "class": "works_for", "from": b, "to": a}],
            }
        )


def test_geometric_shape_forbidden_on_text_items() -> None:
    with pytest.raises(ValidationError, match="text"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "text",
                "shapes": [{"id": _uid(), "type": "point", "class": "defect", "point": [1, 2]}],
            }
        )


def test_span_and_relation_valid_on_text_items() -> None:
    s1, s2 = _uid(), _uid()
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [
                {"id": s1, "type": "span", "class": "PER", "start": 0, "end": 5, "text": "Alice"},
                {"id": s2, "type": "span", "class": "PER", "start": 15, "end": 21},
                {"id": _uid(), "type": "relation", "class": "works_for", "from": s1, "to": s2},
            ],
        }
    )
    assert len(result.shapes) == 3


def test_relation_from_and_to_must_differ() -> None:
    same = _uid()
    with pytest.raises(ValidationError, match="differ"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "text",
                "shapes": [
                    {
                        "id": _uid(),
                        "type": "relation",
                        "class": "works_for",
                        "from": same,
                        "to": same,
                    }
                ],
            }
        )


def test_span_end_must_be_greater_than_start() -> None:
    with pytest.raises(ValidationError, match="end"):
        AnnotationResult.model_validate(
            {
                "schema_version": 1,
                "media_type": "text",
                "shapes": [{"id": _uid(), "type": "span", "class": "PER", "start": 5, "end": 5}],
            }
        )


# ---------------------------------------------------------------------------
# validate_against_schema (QA-6)
# ---------------------------------------------------------------------------


def test_validate_against_schema_flags_dangling_relation_id() -> None:
    s1 = _uid()
    dangling = _uid()
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [
                {"id": s1, "type": "span", "class": "PER", "start": 0, "end": 5},
                {
                    "id": _uid(),
                    "type": "relation",
                    "class": "works_for",
                    "from": s1,
                    "to": dangling,
                },
            ],
        }
    )
    violations = validate_against_schema(result, _text_definition())
    assert any("does not exist" in v for v in violations)


def test_validate_against_schema_relation_to_a_relation_is_dangling() -> None:
    s1, s2 = _uid(), _uid()
    r1 = _uid()
    # `r2` names `r1` (a relation, not a span/point) as its `to`: not a valid endpoint.
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [
                {"id": s1, "type": "span", "class": "PER", "start": 0, "end": 5},
                {"id": s2, "type": "span", "class": "PER", "start": 10, "end": 15},
                {"id": r1, "type": "relation", "class": "works_for", "from": s1, "to": s2},
                {"id": _uid(), "type": "relation", "class": "works_for", "from": s1, "to": r1},
            ],
        }
    )
    violations = validate_against_schema(result, _text_definition())
    assert any("does not exist" in v for v in violations)


def test_validate_against_schema_ok_for_well_formed_text_result() -> None:
    s1, s2 = _uid(), _uid()
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [
                {"id": s1, "type": "span", "class": "PER", "start": 0, "end": 5},
                {"id": s2, "type": "span", "class": "PER", "start": 10, "end": 15},
                {"id": _uid(), "type": "relation", "class": "works_for", "from": s1, "to": s2},
            ],
        }
    )
    assert validate_against_schema(result, _text_definition()) == []


def test_validate_against_schema_span_tool_not_allowed_for_class() -> None:
    definition = LabelSchemaDefinition(
        version=1,
        classes=[
            ClassDef(name="PER", display_name="Person", color="#e11d48", tools=[ToolType.BBOX])
        ],
    )
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [{"id": _uid(), "type": "span", "class": "PER", "start": 0, "end": 5}],
        }
    )
    violations = validate_against_schema(result, definition)
    assert any("tool" in v for v in violations)
