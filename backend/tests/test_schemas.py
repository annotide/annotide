"""Tests for app.schemas: label schema validation, annotation results, QA-6."""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.annotation import (
    AnnotationResult,
    BBoxShape,
    MaskShape,
    PointShape,
    PolygonShape,
    PolylineShape,
    RBoxShape,
    validate_against_schema,
)
from app.schemas.auth import UserRead
from app.schemas.common import Page, ProblemDetail
from app.schemas.label_schema import (
    AttributeDef,
    ClassDef,
    LabelSchemaDefinition,
)

# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def _car_class(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "car",
        "display_name": "Car",
        "color": "#e11d48",
        "hotkey": "1",
        "tools": ["bbox", "polygon"],
        "attributes": [
            {"name": "occluded", "type": "boolean", "required": False, "default": False},
            {
                "name": "make",
                "type": "select",
                "required": True,
                "options": ["Toyota", "Volvo", "Other"],
            },
        ],
    }
    base.update(overrides)
    return base


def _road_class(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "road",
        "display_name": "Road",
        "color": "#22c55e",
        "hotkey": "2",
        "tools": ["polygon", "polyline"],
        "attributes": [],
    }
    base.update(overrides)
    return base


def _definition_data(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "classes": [_car_class(), _road_class()],
        "classification": [
            {
                "name": "weather",
                "type": "select",
                "required": True,
                "options": ["clear", "rain", "snow"],
            }
        ],
    }
    base.update(overrides)
    return base


def build_definition() -> LabelSchemaDefinition:
    return LabelSchemaDefinition.model_validate(_definition_data())


def _result_data(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "media_type": "image",
        "classification": {"weather": "rain"},
        "shapes": [
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "attributes": {"occluded": True, "make": "Toyota"},
                "bbox": [10.0, 10.0, 50.0, 60.0],
            }
        ],
    }
    base.update(overrides)
    return base


def build_result(**overrides: object) -> AnnotationResult:
    return AnnotationResult.model_validate(_result_data(**overrides))


# ---------------------------------------------------------------------------
# common.py
# ---------------------------------------------------------------------------


def test_page_envelope_matches_contract_shape() -> None:
    page = Page[int](items=[1, 2, 3], next_cursor="abc")
    assert page.model_dump() == {"items": [1, 2, 3], "next_cursor": "abc"}


def test_user_read_exposes_superuser_flag_but_never_the_password_hash() -> None:
    """The UI needs `is_superuser` to show admin pages; `password_hash` never leaves the API."""
    now = "2026-09-18T00:00:00Z"
    row = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="a@example.com",
        display_name="A",
        idp_subject=None,
        is_active=True,
        is_superuser=True,
        last_seen_at=None,
        created_at=now,
        updated_at=now,
        password_hash="should-not-appear",
    )
    user = UserRead.model_validate(row, from_attributes=True)
    dumped = user.model_dump()
    assert dumped["is_superuser"] is True
    assert "password_hash" not in dumped


def test_problem_detail_matches_rfc9457_fields() -> None:
    problem = ProblemDetail(title="Not Found", status=404, detail="item missing")
    assert problem.model_dump() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "item missing",
    }


# ---------------------------------------------------------------------------
# label_schema.py — valid schema
# ---------------------------------------------------------------------------


def test_label_schema_valid() -> None:
    definition = build_definition()
    assert len(definition.classes) == 2
    assert definition.classes[0].name == "car"
    assert definition.classification[0].name == "weather"


# ---------------------------------------------------------------------------
# label_schema.py — one negative test per validation rule
# ---------------------------------------------------------------------------


def test_label_schema_duplicate_class_names_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        LabelSchemaDefinition.model_validate(_definition_data(classes=[_car_class(), _car_class()]))


def test_label_schema_select_requires_non_empty_options() -> None:
    with pytest.raises(ValidationError, match="requires non-empty options"):
        AttributeDef.model_validate({"name": "make", "type": "select", "options": []})


def test_label_schema_non_select_forbids_options() -> None:
    with pytest.raises(ValidationError, match="must not declare options"):
        AttributeDef.model_validate({"name": "occluded", "type": "boolean", "options": ["a"]})


def test_label_schema_invalid_color_rejected() -> None:
    with pytest.raises(ValidationError, match="color"):
        ClassDef.model_validate(_car_class(color="red"))


def test_label_schema_hotkey_must_be_single_character() -> None:
    with pytest.raises(ValidationError, match="single character"):
        ClassDef.model_validate(_car_class(hotkey="12"))


def test_label_schema_duplicate_hotkeys_rejected() -> None:
    with pytest.raises(ValidationError, match="hotkeys must be unique"):
        LabelSchemaDefinition.model_validate(
            _definition_data(classes=[_car_class(hotkey="1"), _road_class(hotkey="1")])
        )


def test_label_schema_class_requires_at_least_one_tool() -> None:
    with pytest.raises(ValidationError, match="at least one tool"):
        ClassDef.model_validate(_car_class(tools=[]))


# ---------------------------------------------------------------------------
# annotation.py — valid results
# ---------------------------------------------------------------------------


def test_bbox_shape_valid() -> None:
    shape = BBoxShape.model_validate(
        {"id": str(uuid.uuid4()), "type": "bbox", "class": "car", "bbox": [10.0, 10.0, 50.0, 60.0]}
    )
    assert shape.class_ == "car"
    assert shape.bbox == (10.0, 10.0, 50.0, 60.0)


def test_annotation_result_valid_with_all_shape_kinds() -> None:
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "classification": {"weather": "rain"},
            "shapes": [
                {
                    "id": "b3f1c2a0-0000-4000-8000-000000000000",
                    "type": "bbox",
                    "class": "car",
                    "attributes": {"occluded": True},
                    "confidence": None,
                    "bbox": [120.0, 84.5, 310.0, 240.0],
                },
                {
                    "id": "b3f1c2a0-0000-4000-8000-000000000001",
                    "type": "polygon",
                    "class": "road",
                    "attributes": {},
                    "points": [[10, 10], [200, 10], [200, 90]],
                },
                {
                    "id": "b3f1c2a0-0000-4000-8000-000000000002",
                    "type": "polyline",
                    "class": "road",
                    "points": [[0, 0], [10, 10]],
                },
                {
                    "id": "b3f1c2a0-0000-4000-8000-000000000003",
                    "type": "point",
                    "class": "car",
                    "point": [55.0, 12.0],
                },
                {
                    "id": "b3f1c2a0-0000-4000-8000-000000000004",
                    "type": "mask",
                    "class": "road",
                    "rle": {"size": [10, 10], "counts": [0, 100]},
                },
            ],
        }
    )
    assert len(result.shapes) == 5


def test_annotation_result_round_trips_class_alias() -> None:
    result = build_result()
    dumped = result.model_dump(mode="json", by_alias=True)
    assert dumped["shapes"][0]["class"] == "car"
    assert "class_" not in dumped["shapes"][0]


def test_mask_shape_valid() -> None:
    shape = MaskShape.model_validate(
        {
            "id": str(uuid.uuid4()),
            "type": "mask",
            "class": "road",
            "rle": {"size": [10, 10], "counts": [0, 100]},
        }
    )
    assert shape.rle.counts == [0, 100]
    assert shape.rle.size == (10, 10)


# ---------------------------------------------------------------------------
# annotation.py — QA-6 negative tests
# ---------------------------------------------------------------------------


def test_bbox_shape_rejects_non_positive_extent() -> None:
    with pytest.raises(ValidationError, match="x_max must be greater than x_min"):
        BBoxShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "bbox": [10.0, 10.0, 10.0, 60.0],
            }
        )


def test_bbox_shape_rejects_negative_coordinates() -> None:
    with pytest.raises(ValidationError, match=">= 0"):
        BBoxShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "bbox": [-1.0, 10.0, 50.0, 60.0],
            }
        )


def test_polygon_requires_at_least_three_points() -> None:
    with pytest.raises(ValidationError, match="at least 3 points"):
        PolygonShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "polygon",
                "class": "road",
                "points": [[0, 0], [1, 1]],
            }
        )


def test_rbox_corners_and_validation() -> None:
    shape = RBoxShape.model_validate(
        {
            "id": str(uuid.uuid4()),
            "type": "rbox",
            "class": "ship",
            "center": [10, 10],
            "size": [4, 2],
            "angle": 0,
        }
    )
    assert shape.corners() == [(8, 9), (12, 9), (12, 11), (8, 11)]

    with pytest.raises(ValidationError, match="size must be greater than 0"):
        RBoxShape.model_validate(
            {
                "id": "x",
                "type": "rbox",
                "class": "ship",
                "center": [1, 1],
                "size": [0, 2],
                "angle": 0,
            }
        )
    with pytest.raises(ValidationError, match=r"\(-180, 180\]"):
        RBoxShape.model_validate(
            {
                "id": "x",
                "type": "rbox",
                "class": "ship",
                "center": [1, 1],
                "size": [2, 2],
                "angle": 270,
            }
        )


def test_polyline_requires_at_least_two_points() -> None:
    with pytest.raises(ValidationError, match="at least 2 points"):
        PolylineShape.model_validate(
            {"id": str(uuid.uuid4()), "type": "polyline", "class": "road", "points": [[0, 0]]}
        )


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError, match=r"between 0\.0 and 1\.0"):
        PointShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "point",
                "class": "car",
                "point": [1.0, 2.0],
                "confidence": 1.5,
            }
        )


def test_shape_id_must_be_a_uuid() -> None:
    with pytest.raises(ValidationError):
        PointShape.model_validate(
            {"id": "not-a-uuid", "type": "point", "class": "car", "point": [1.0, 2.0]}
        )


# ---------------------------------------------------------------------------
# validate_against_schema (QA-6) — table of cases
# ---------------------------------------------------------------------------


def test_validate_against_schema_valid_result_has_no_violations() -> None:
    violations = validate_against_schema(build_result(), build_definition())
    assert violations == []


def test_validate_against_schema_unknown_class() -> None:
    result = build_result(
        shapes=[
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "truck",
                "attributes": {},
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ]
    )
    violations = validate_against_schema(result, build_definition())
    assert any("unknown class" in v for v in violations)


def test_validate_against_schema_tool_not_allowed_for_class() -> None:
    # "road" only allows polygon/polyline, not bbox
    result = build_result(
        shapes=[
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "road",
                "attributes": {},
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ]
    )
    violations = validate_against_schema(result, build_definition())
    assert any("not allowed" in v for v in violations)


def test_validate_against_schema_missing_required_attribute() -> None:
    result = build_result(
        shapes=[
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "attributes": {},  # "make" is required on "car"
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ]
    )
    violations = validate_against_schema(result, build_definition())
    assert any("missing required attribute 'make'" in v for v in violations)


def test_validate_against_schema_attribute_value_not_in_options() -> None:
    result = build_result(
        shapes=[
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "attributes": {"make": "Tesla"},
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ]
    )
    violations = validate_against_schema(result, build_definition())
    assert any("not in options" in v for v in violations)


def test_validate_against_schema_wrong_attribute_type() -> None:
    result = build_result(
        shapes=[
            {
                "id": str(uuid.uuid4()),
                "type": "bbox",
                "class": "car",
                "attributes": {"occluded": "yes", "make": "Toyota"},
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ]
    )
    violations = validate_against_schema(result, build_definition())
    assert any("must be a boolean" in v for v in violations)


def test_validate_against_schema_unknown_classification_attribute() -> None:
    result = build_result(classification={"temperature": 20})
    violations = validate_against_schema(result, build_definition())
    assert any("unknown classification attribute" in v for v in violations)


def test_validate_against_schema_missing_required_classification() -> None:
    result = build_result(classification={})
    violations = validate_against_schema(result, build_definition())
    assert any("missing required classification attribute 'weather'" in v for v in violations)


def test_validate_against_schema_classification_value_not_in_options() -> None:
    result = build_result(classification={"weather": "hail"})
    violations = validate_against_schema(result, build_definition())
    assert any("not in options" in v for v in violations)
