"""Tests for `services/agreement.py` (QA-2): hand-computed metric values.

Fleiss' kappa, Cohen's kappa and Krippendorff's alpha are checked against
values worked out by hand (see the docstring of each test for the
arithmetic), not merely re-derived with the same formula, so a
transcription bug in the implementation would actually be caught.
"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from app.schemas.annotation import (
    AnnotationResult,
    BBoxShape,
    MaskRLE,
    MaskShape,
    PointShape,
    Shape,
)
from app.schemas.item import MediaType
from app.services import agreement


def _bbox(cls: str, coords: tuple[float, float, float, float]) -> BBoxShape:
    return BBoxShape(id=uuid4(), class_=cls, bbox=coords)


def _mask(size: tuple[int, int], counts: list[int]) -> MaskShape:
    return MaskShape(id=uuid4(), class_="thing", rle=MaskRLE(size=size, counts=counts))


def _point(cls: str, xy: tuple[float, float]) -> PointShape:
    return PointShape(id=uuid4(), class_=cls, point=xy)


def _result(*shapes: object, classification: dict[str, object] | None = None) -> AnnotationResult:
    return AnnotationResult(
        schema_version=1,
        media_type=MediaType.IMAGE,
        classification=classification or {},
        shapes=cast(list[Shape], list(shapes)),
    )


class TestCohenKappa:
    def test_known_two_by_two_table(self) -> None:
        """Rater1/Rater2 2x2 table a=5 (A,A), b=2 (A,B), c=2 (B,A), d=5 (B,B).

        po = (5+5)/14 = 5/7; marginals are 7/14 = 1/2 each way, so
        pe = 0.5*0.5 + 0.5*0.5 = 0.5. kappa = (5/7 - 1/2)/(1/2) = 3/7.
        """
        pairs = [("A", "A")] * 5 + [("A", "B")] * 2 + [("B", "A")] * 2 + [("B", "B")] * 5
        kappa = agreement.cohen_kappa(pairs)
        assert kappa is not None
        assert kappa == pytest.approx(3 / 7)

    def test_fewer_than_two_pairs_is_none(self) -> None:
        assert agreement.cohen_kappa([("A", "A")]) is None
        assert agreement.cohen_kappa([]) is None

    def test_perfect_agreement_is_one(self) -> None:
        pairs = [("A", "A"), ("B", "B"), ("A", "A")]
        assert agreement.cohen_kappa(pairs) == pytest.approx(1.0)


class TestFleissKappa:
    def test_known_three_item_three_rater_example(self) -> None:
        """3 raters, 3 items, 2 categories; kappa = 22/40 = 0.55 (worked by hand).

        Item1=[A,A,A] P1=1; Item2=[A,A,B] P2=1/3; Item3=[B,B,B] P3=1.
        P_bar = 7/9. p_A=5/9, p_B=4/9 -> P_e = 41/81.
        kappa = (7/9 - 41/81)/(1 - 41/81) = (22/81)/(40/81) = 22/40 = 0.55.
        """
        rows = [["A", "A", "A"], ["A", "A", "B"], ["B", "B", "B"]]
        kappa = agreement.fleiss_kappa(rows)
        assert kappa is not None
        assert kappa == pytest.approx(0.55)

    def test_modal_rater_count_excludes_short_rows(self) -> None:
        """Two 3-rater rows dominate; a 2-rater row is left out of the computation."""
        rows = [["A", "A", "A"], ["B", "B", "B"], ["A", "B"]]
        kappa = agreement.fleiss_kappa(rows)
        # Only the two 3-rater, unanimous rows are used -> perfect agreement.
        assert kappa == pytest.approx(1.0)

    def test_empty_is_none(self) -> None:
        assert agreement.fleiss_kappa([]) is None
        assert agreement.fleiss_kappa([[], []]) is None


class TestKrippendorffAlpha:
    def test_perfect_agreement_is_one(self) -> None:
        units = [["A", "A", "A"], ["B", "B"]]
        assert agreement.krippendorff_alpha_nominal(units) == pytest.approx(1.0)

    def test_known_missing_data_example(self) -> None:
        """U1=[A,A,B] (3 raters), U2=[A,A] (2 raters), U3=[B] (1 rater, excluded).

        Coincidence matrix: o[A,A]=1+2=3, o[A,B]=o[B,A]=1, o[B,B]=0.
        n_A=4, n_B=1, n_total=5. Do_num=2, De_num=2*4*1=8.
        alpha = 1 - 2*(5-1)/8 = 1 - 1 = 0.0.
        """
        units = [["A", "A", "B"], ["A", "A"], ["B"]]
        alpha = agreement.krippendorff_alpha_nominal(units)
        assert alpha == pytest.approx(0.0)

    def test_single_category_is_none(self) -> None:
        assert agreement.krippendorff_alpha_nominal([["A", "A"], ["A"]]) is None


class TestEnvelopeIou:
    def test_known_bbox_overlap(self) -> None:
        """(0,0,10,10) vs (5,5,15,15): intersection 25, union 175, iou = 1/7."""
        a = _bbox("car", (0, 0, 10, 10))
        b = _bbox("car", (5, 5, 15, 15))
        assert agreement.envelope_iou(a, b) == pytest.approx(1 / 7)

    def test_no_overlap_is_zero(self) -> None:
        a = _bbox("car", (0, 0, 1, 1))
        b = _bbox("car", (10, 10, 11, 11))
        assert agreement.envelope_iou(a, b) == 0.0


class TestMaskIou:
    def test_known_pixel_overlap(self) -> None:
        # 2x2 mask, column-major: background(1),fg(1),bg(1),fg(1) -> [0,1,0,1]
        a = _mask((2, 2), [1, 1, 1, 1])
        # [0,1,1,0]
        b = _mask((2, 2), [1, 2, 1])
        iou = agreement.mask_iou(a.rle, b.rle)
        # a pixels: [0,1,0,1]; b pixels: [0,1,1,0]
        # intersection at index 1 only -> 1; union at indices 1,2,3 -> 3
        assert iou == pytest.approx(1 / 3)

    def test_size_mismatch_is_zero(self) -> None:
        a = _mask((2, 2), [4])
        b = _mask((3, 3), [9])
        assert agreement.mask_iou(a.rle, b.rle) == 0.0


class TestMatchShapes:
    def test_matched_pair_counts_and_iou(self) -> None:
        a = [_bbox("car", (0, 0, 10, 10))]
        b = [_bbox("car", (5, 5, 15, 15))]
        result = agreement.match_shapes(a, b, iou_threshold=0.1)
        assert result.matched == 1
        assert result.total_a == 1
        assert result.total_b == 1
        assert agreement.mean_iou(result) == pytest.approx(1 / 7)
        assert agreement.shape_f1(result) == pytest.approx(1.0)

    def test_below_threshold_does_not_match(self) -> None:
        a = [_bbox("car", (0, 0, 10, 10))]
        b = [_bbox("car", (5, 5, 15, 15))]
        result = agreement.match_shapes(a, b, iou_threshold=0.5)
        assert result.matched == 0
        assert agreement.shape_f1(result) == 0.0
        assert agreement.mean_iou(result) is None

    def test_different_class_never_matches(self) -> None:
        a = [_bbox("car", (0, 0, 10, 10))]
        b = [_bbox("bike", (0, 0, 10, 10))]
        result = agreement.match_shapes(a, b, iou_threshold=0.5)
        assert result.matched == 0

    def test_point_within_tolerance_matches(self) -> None:
        a = [_point("cone", (10, 10))]
        b = [_point("cone", (15, 10))]  # distance 5 <= 10px tolerance
        result = agreement.match_shapes(a, b, iou_threshold=0.5)
        assert result.matched == 1
        # point matches do not contribute a "genuine" IoU
        assert agreement.mean_iou(result) is None

    def test_point_outside_tolerance_does_not_match(self) -> None:
        a = [_point("cone", (0, 0))]
        b = [_point("cone", (100, 0))]
        result = agreement.match_shapes(a, b, iou_threshold=0.5)
        assert result.matched == 0

    def test_empty_both_sides_f1_is_none(self) -> None:
        result = agreement.match_shapes([], [], iou_threshold=0.5)
        assert agreement.shape_f1(result) is None


class TestMatchSpans:
    def test_exact_and_overlap(self) -> None:
        from app.schemas.annotation import SpanShape

        a = [SpanShape(id=uuid4(), class_="entity", start=0, end=5)]
        b = [SpanShape(id=uuid4(), class_="entity", start=0, end=5)]
        result = agreement.match_spans(a, b)
        assert result.exact_matched == 1
        assert result.overlap_matched == 1
        assert agreement.span_f1_exact(result) == pytest.approx(1.0)
        assert agreement.span_f1_overlap(result) == pytest.approx(1.0)

    def test_overlap_only(self) -> None:
        from app.schemas.annotation import SpanShape

        a = [SpanShape(id=uuid4(), class_="entity", start=0, end=5)]
        b = [SpanShape(id=uuid4(), class_="entity", start=3, end=8)]
        result = agreement.match_spans(a, b)
        assert result.exact_matched == 0
        assert result.overlap_matched == 1
        assert agreement.span_f1_exact(result) == 0.0
        assert agreement.span_f1_overlap(result) == pytest.approx(1.0)


class TestComputeItemAgreement:
    def test_pooled_classification_and_shapes(self) -> None:
        user_a, user_b, user_c = uuid4(), uuid4(), uuid4()
        versions = {
            user_a: _result(_bbox("car", (0, 0, 10, 10)), classification={"weather": "sunny"}),
            user_b: _result(_bbox("car", (0, 0, 10, 10)), classification={"weather": "sunny"}),
            user_c: _result(_bbox("car", (0, 0, 10, 10)), classification={"weather": "rainy"}),
        }
        result = agreement.compute_item_agreement(versions, iou_threshold=0.5)
        assert result.shapes.f1 == pytest.approx(1.0)
        assert result.shapes.mean_iou == pytest.approx(1.0)
        assert len(result.pairs) == 3  # 3 annotators -> 3 pairs
        assert all(pair.items is None for pair in result.pairs)
        [field] = result.classification
        assert field.field == "weather"


class TestGoldScore:
    def test_classification_accuracy(self) -> None:
        attempt = _result(classification={"weather": "sunny", "time": "day"})
        reference = _result(classification={"weather": "sunny", "time": "night"})
        acc = agreement.classification_accuracy(attempt.classification, reference.classification)
        assert acc == pytest.approx(0.5)

    def test_gold_score_combines_metrics(self) -> None:
        attempt = _result(_bbox("car", (0, 0, 10, 10)), classification={"weather": "sunny"})
        reference = _result(_bbox("car", (0, 0, 10, 10)), classification={"weather": "sunny"})
        score = agreement.gold_score(attempt, reference)
        assert score.classification_accuracy == pytest.approx(1.0)
        assert score.shape_f1 == pytest.approx(1.0)
        assert score.mean_iou == pytest.approx(1.0)
