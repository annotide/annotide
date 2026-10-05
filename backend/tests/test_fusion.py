"""Tests for `services/fusion.py` (QA-3)."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from app.schemas.annotation import (
    AnnotationResult,
    BBoxShape,
    PointShape,
    RelationShape,
    Shape,
    SpanShape,
)
from app.schemas.item import MediaType
from app.services.fusion import fuse


def _bbox(
    cls: str, coords: tuple[float, float, float, float], confidence: float | None = None
) -> BBoxShape:
    return BBoxShape(id=uuid4(), class_=cls, bbox=coords, confidence=confidence)


def _result(*shapes: object, classification: dict[str, object] | None = None) -> AnnotationResult:
    # Spans and relations only live on text items (CONTRACTS.md, text items).
    is_text = any(isinstance(shape, SpanShape | RelationShape) for shape in shapes)
    return AnnotationResult(
        schema_version=1,
        media_type=MediaType.TEXT if is_text else MediaType.IMAGE,
        classification=classification or {},
        shapes=cast(list[Shape], list(shapes)),
    )


class TestFuseClassification:
    def test_majority_wins_with_enough_votes(self) -> None:
        results = [
            _result(classification={"weather": "sunny"}),
            _result(classification={"weather": "sunny"}),
            _result(classification={"weather": "rainy"}),
        ]
        fused, conflicts = fuse(results)
        assert fused.classification == {"weather": "sunny"}
        assert conflicts == []

    def test_tie_is_a_conflict(self) -> None:
        results = [
            _result(classification={"weather": "sunny"}),
            _result(classification={"weather": "rainy"}),
        ]
        fused, conflicts = fuse(results, min_votes=1)
        # Tie between two equally-voted values -> unset, and reported.
        assert "weather" not in fused.classification
        assert "classification.weather" in conflicts

    def test_default_min_votes_is_ceil_half(self) -> None:
        # N=4 -> default min_votes = 2; 2 votes for "sunny" is enough.
        results = [
            _result(classification={"weather": "sunny"}),
            _result(classification={"weather": "sunny"}),
            _result(classification={"weather": "rainy"}),
            _result(classification={"weather": "cloudy"}),
        ]
        fused, conflicts = fuse(results)
        assert fused.classification == {"weather": "sunny"}
        assert conflicts == []

    def test_multiselect_order_independent(self) -> None:
        results = [
            _result(classification={"tags": ["a", "b"]}),
            _result(classification={"tags": ["b", "a"]}),
        ]
        fused, conflicts = fuse(results, min_votes=1)
        assert set(fused.classification["tags"]) == {"a", "b"}
        assert conflicts == []


class TestFuseShapes:
    def test_bbox_cluster_mean(self) -> None:
        results = [
            _result(_bbox("car", (0, 0, 10, 10))),
            _result(_bbox("car", (2, 2, 12, 12))),
        ]
        fused, conflicts = fuse(results, iou_threshold=0.1, min_votes=2)
        assert conflicts == []
        [shape] = fused.shapes
        assert isinstance(shape, BBoxShape)
        assert shape.bbox == pytest.approx((1.0, 1.0, 11.0, 11.0))
        assert shape.confidence is None

    def test_confidence_weighted_mean(self) -> None:
        results = [
            _result(_bbox("car", (0, 0, 10, 10), confidence=0.25)),
            _result(_bbox("car", (10, 10, 20, 20), confidence=0.75)),
        ]
        fused, _ = fuse(results, iou_threshold=0.0, min_votes=1)
        [shape] = fused.shapes
        assert isinstance(shape, BBoxShape)
        # weighted mean: 0.25*0 + 0.75*10 = 7.5 for x_min etc.
        assert shape.bbox[0] == pytest.approx(7.5)

    def test_below_min_votes_is_dropped_and_conflict_reported(self) -> None:
        results = [
            _result(_bbox("car", (0, 0, 10, 10))),
            _result(_bbox("car", (100, 100, 110, 110))),  # does not cluster with the first
        ]
        fused, conflicts = fuse(results, iou_threshold=0.5, min_votes=2)
        assert fused.shapes == []
        assert conflicts.count("shape.car") == 2

    def test_new_ids_assigned(self) -> None:
        original_id = uuid4()
        results = [
            _result(BBoxShape(id=original_id, class_="car", bbox=(0, 0, 10, 10))),
            _result(_bbox("car", (1, 1, 11, 11))),
        ]
        fused, _ = fuse(results, iou_threshold=0.1, min_votes=1)
        assert all(shape.id != original_id for shape in fused.shapes)

    def test_point_mean(self) -> None:
        results = [
            _result(PointShape(id=uuid4(), class_="cone", point=(0, 0))),
            _result(PointShape(id=uuid4(), class_="cone", point=(10, 0))),
        ]
        fused, conflicts = fuse(results, min_votes=2)
        assert conflicts == []
        [shape] = fused.shapes
        assert isinstance(shape, PointShape)
        assert shape.point == pytest.approx((5.0, 0.0))


class TestFuseSpans:
    def test_exact_span_cluster(self) -> None:
        results = [
            _result(SpanShape(id=uuid4(), class_="entity", start=0, end=5)),
            _result(SpanShape(id=uuid4(), class_="entity", start=0, end=5)),
        ]
        fused, conflicts = fuse(results, min_votes=2)
        assert conflicts == []
        [span] = fused.shapes
        assert isinstance(span, SpanShape)
        assert (span.start, span.end) == (0, 5)

    def test_span_below_min_votes_conflicts(self) -> None:
        results = [
            _result(SpanShape(id=uuid4(), class_="entity", start=0, end=5)),
            _result(SpanShape(id=uuid4(), class_="entity", start=10, end=15)),
        ]
        fused, conflicts = fuse(results, min_votes=2)
        assert fused.shapes == []
        assert "span.entity" in conflicts


class TestFuseRelations:
    def test_relation_kept_when_ends_fuse(self) -> None:
        def _annotator() -> AnnotationResult:
            per = SpanShape(id=uuid4(), class_="PER", start=0, end=5)
            org = SpanShape(id=uuid4(), class_="ORG", start=15, end=21)
            works = RelationShape(id=uuid4(), class_="works_for", **{"from": per.id, "to": org.id})
            return _result(per, org, works)

        fused, conflicts = fuse([_annotator(), _annotator()], iou_threshold=0.5, min_votes=2)
        spans = {s.id: s for s in fused.shapes if isinstance(s, SpanShape)}
        relations = [s for s in fused.shapes if isinstance(s, RelationShape)]
        assert len(relations) == 1
        assert spans[relations[0].from_].class_ == "PER"
        assert spans[relations[0].to].class_ == "ORG"
        assert not any(c.startswith("relation") for c in conflicts)


class TestFuseErrors:
    def test_empty_results_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one result"):
            fuse([])
