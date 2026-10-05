"""`services/uncertainty.py` (ML-6)."""

from __future__ import annotations

import uuid
from typing import Any

from app.schemas import AnnotationResult
from app.services.uncertainty import uncertainty_priority, uncertainty_score


def _result(*shapes: dict[str, Any]) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": "image", "classification": {}, "shapes": list(shapes)}
    )


def _bbox(confidence: float | None) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "type": "bbox",
        "class": "car",
        "bbox": [1, 2, 30, 40],
    }
    if confidence is not None:
        shape["confidence"] = confidence
    return shape


class TestUncertaintyScore:
    def test_empty_prediction_is_maximally_uncertain(self) -> None:
        assert uncertainty_score(None) == 1.0
        assert uncertainty_score(_result()) == 1.0

    def test_least_confidence_over_shapes(self) -> None:
        assert uncertainty_score(_result(_bbox(0.9), _bbox(0.3), _bbox(0.7))) == 0.7
        assert uncertainty_score(_result(_bbox(1.0))) == 0.0

    def test_shapes_without_confidence_are_half_uncertain(self) -> None:
        assert uncertainty_score(_result(_bbox(None))) == 0.5
        # ... but known confidences win over unknown ones.
        assert uncertainty_score(_result(_bbox(None), _bbox(0.8))) == 0.2


class TestUncertaintyPriority:
    def test_scales_to_0_100_and_clamps(self) -> None:
        assert uncertainty_priority(0.0) == 0
        assert uncertainty_priority(0.456) == 46
        assert uncertainty_priority(1.0) == 100
        assert uncertainty_priority(1.7) == 100
        assert uncertainty_priority(-2.0) == 0
