"""Inter-annotator agreement metrics (QA-2): pure functions over `AnnotationResult`s.

No numpy / shapely (no new runtime dependency): IoU is plain Python geometry —
axis-aligned envelope for `bbox` / `rbox` / `polygon` (documented as an
approximation for the latter two), exact pixel IoU for `mask` via its RLE, a
distance tolerance for `point`. Classification agreement is Cohen's kappa
(pairwise), Fleiss' kappa and Krippendorff's alpha (nominal, missing values
allowed). See CONTRACTS.md *Quality control*.

Two design choices not spelled out verbatim by the contract:

- `mean_iou` only pools matches that carry a genuine geometric IoU (`bbox`,
  `rbox`, `polygon`, `mask`); `point` and `relation` matches are binary
  (matched or not) and contribute to `shape_f1`'s numerator/denominator only.
- `ItemAgreement` (one item) omits the top-level `annotators` list the
  contract shows on `ProjectAgreement` — the endpoint that embeds it
  (`GET /items/{id}/consensus`) already returns its own, differently-shaped
  `annotators` array alongside `agreement`.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Literal
from uuid import UUID

from app.schemas.annotation import (
    AnnotationResult,
    BBoxShape,
    KeypointsShape,
    MaskRLE,
    MaskShape,
    PointShape,
    PolygonShape,
    RankingShape,
    RatingShape,
    RBoxShape,
    RelationShape,
    SegmentShape,
    Shape,
    ShapeBase,
    SpanShape,
)
from app.schemas.quality import (
    ClassificationAgreement,
    ItemAgreement,
    PairAgreement,
    ShapeAgreement,
    SpanAgreement,
)

#: Matching tolerance for `point` shapes, in pixels (CONTRACTS.md *Quality control*).
POINT_TOLERANCE_PX = 10.0

#: Per-keypoint falloff of the object keypoint similarity (COCO OKS `k`), one
#: value for every point since a custom skeleton has no published constants.
OKS_KAPPA = 0.1
#: Smallest object scale `s²` (px²) in OKS, so a skeleton with one or two
#: labelled points (a zero-area envelope) can still match.
OKS_MIN_SCALE = 32.0 * 32.0

ShapeCategory = Literal["box", "mask", "point", "keypoints", "relation", "interval"]


def normalize_value(value: Any) -> Any:
    """A classification value in a form that is hashable and order-independent.

    `multiselect` values are lists; two annotators who pick the same set in a
    different order must still count as agreeing.
    """
    if isinstance(value, list):
        return tuple(sorted(value, key=str))
    return value


# --------------------------------------------------------------------------
# Classification agreement
# --------------------------------------------------------------------------


def cohen_kappa(pairs: list[tuple[Any, Any]]) -> float | None:
    """Pairwise agreement between two raters over paired, non-missing values.

    `None` when there are fewer than 2 pairs, or expected agreement is 1
    (nothing left to explain).
    """
    if len(pairs) < 2:
        return None
    n = len(pairs)
    a_counts: Counter[Any] = Counter(a for a, _ in pairs)
    b_counts: Counter[Any] = Counter(b for _, b in pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    categories = set(a_counts) | set(b_counts)
    pe = sum((a_counts[c] / n) * (b_counts[c] / n) for c in categories)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def fleiss_kappa(rows: list[list[Any]]) -> float | None:
    """Fleiss' kappa over items with the modal number of raters (QA-2).

    `rows` holds one list of values per item — one entry per rater who gave a
    value on that item. Items whose rater count differs from the mode are
    left out, since Fleiss' formula needs a fixed `n` per unit; empty rows are
    always ignored. `None` when fewer than 2 usable items, fewer than 2
    raters, or expected agreement is 1.
    """
    sized = [row for row in rows if row]
    if not sized:
        return None
    counts = Counter(len(row) for row in sized)
    modal_n = counts.most_common(1)[0][0]
    selected = [row for row in sized if len(row) == modal_n]
    if modal_n < 2 or len(selected) < 2:
        return None

    categories = sorted({normalize_value(v) for row in selected for v in row}, key=str)
    n = modal_n
    big_n = len(selected)
    p_i: list[float] = []
    category_totals: dict[Any, int] = dict.fromkeys(categories, 0)
    for row in selected:
        row_counts = Counter(normalize_value(v) for v in row)
        for category in categories:
            category_totals[category] += row_counts[category]
        sum_sq = sum(row_counts[c] ** 2 for c in categories)
        p_i.append((sum_sq - n) / (n * (n - 1)))
    p_bar = sum(p_i) / big_n
    p_e = sum((category_totals[c] / (big_n * n)) ** 2 for c in categories)
    if p_e >= 1.0:
        return None
    return (p_bar - p_e) / (1 - p_e)


def krippendorff_alpha_nominal(units: list[list[Any]]) -> float | None:
    """Krippendorff's alpha (nominal metric), missing values allowed (QA-2).

    `units` holds one list of values per unit (an item, or a classification
    field pooled over items); units with fewer than 2 values are missing data
    and contribute no pairs but do not break the computation. `None` when
    fewer than 2 categories appear overall, or the expected disagreement is 0
    (nothing to normalise against). Computed via the coincidence-matrix form
    of the statistic (Krippendorff, *Computing Krippendorff's Alpha-Reliability*).
    """
    normalized = [[normalize_value(v) for v in unit] for unit in units]
    categories = sorted({v for unit in normalized for v in unit}, key=str)
    if len(categories) < 2:
        return None

    coincidence: dict[tuple[Any, Any], float] = {
        (c, k): 0.0 for c in categories for k in categories
    }
    for unit in normalized:
        n_u = len(unit)
        if n_u < 2:
            continue
        counts = Counter(unit)
        denom = n_u - 1
        for c in categories:
            for k in categories:
                if c == k:
                    coincidence[c, k] += counts[c] * (counts[c] - 1) / denom
                else:
                    coincidence[c, k] += counts[c] * counts[k] / denom

    marginals = {c: sum(coincidence[c, k] for k in categories) for c in categories}
    n_total = sum(marginals.values())
    if n_total < 2:
        return None

    disagreement = sum(coincidence[c, k] for c in categories for k in categories if c != k)
    expected_sum = sum(
        marginals[c] * marginals[k] for c in categories for k in categories if c != k
    )
    if expected_sum == 0:
        return None
    return 1 - disagreement * (n_total - 1) / expected_sum


# --------------------------------------------------------------------------
# Shape matching (bbox / rbox / polygon / mask / point / relation)
# --------------------------------------------------------------------------


def shape_category(shape: ShapeBase) -> ShapeCategory | None:
    """Which matching rule applies to a shape, or `None` for polyline / span."""
    if isinstance(shape, BBoxShape | RBoxShape | PolygonShape):
        return "box"
    if isinstance(shape, MaskShape):
        return "mask"
    if isinstance(shape, PointShape):
        return "point"
    if isinstance(shape, KeypointsShape):
        return "keypoints"
    if isinstance(shape, RelationShape):
        return "relation"
    if isinstance(shape, SegmentShape):
        return "interval"
    return None


def _envelope(shape: ShapeBase) -> tuple[float, float, float, float] | None:
    """Axis-aligned envelope of a `bbox` / `rbox` / `polygon` shape."""
    if isinstance(shape, BBoxShape):
        return shape.bbox
    if isinstance(shape, RBoxShape):
        corners = shape.corners()
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return (min(xs), min(ys), max(xs), max(ys))
    if isinstance(shape, PolygonShape):
        xs = [p[0] for p in shape.points]
        ys = [p[1] for p in shape.points]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def _box_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    intersection = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def envelope_iou(a: ShapeBase, b: ShapeBase) -> float:
    """IoU of two shapes' axis-aligned envelopes (exact for `bbox`, approximate otherwise)."""
    box_a, box_b = _envelope(a), _envelope(b)
    if box_a is None or box_b is None:
        return 0.0
    return _box_iou(box_a, box_b)


def decode_mask(rle: MaskRLE) -> list[int]:
    """The mask as a flat 0/1 array in the RLE's own column-major order."""
    pixels: list[int] = []
    value = 0
    for count in rle.counts:
        pixels.extend([value] * count)
        value = 1 - value
    return pixels


def mask_iou(a: MaskRLE, b: MaskRLE) -> float:
    """Exact pixel IoU of two decoded RLE masks; 0 when their sizes differ."""
    if tuple(a.size) != tuple(b.size):
        return 0.0
    pixels_a = decode_mask(a)
    pixels_b = decode_mask(b)
    length = min(len(pixels_a), len(pixels_b))
    intersection = 0
    union = 0
    for i in range(length):
        pa, pb = pixels_a[i], pixels_b[i]
        if pa and pb:
            intersection += 1
        if pa or pb:
            union += 1
    return intersection / union if union > 0 else 0.0


def point_distance(a: PointShape, b: PointShape) -> float:
    (ax, ay), (bx, by) = a.point, b.point
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)


def keypoint_similarity(a: KeypointsShape, b: KeypointsShape) -> float:
    """Object keypoint similarity (COCO OKS) of two skeletons, symmetric.

    Averaged over the points labelled in either: a point labelled in both
    scores `exp(-d² / (2 s² k²))` with `s²` the mean envelope area of the two
    (at least `OKS_MIN_SCALE`) and `k` = `OKS_KAPPA`; a point labelled in only one
    scores 0. Different point counts (different skeletons) score 0.
    """
    if len(a.points) != len(b.points):
        return 0.0
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a.envelope(), b.envelope()
    scale = max(((ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0)) / 2, OKS_MIN_SCALE)
    denominator = 2 * scale * OKS_KAPPA**2
    total = 0.0
    counted = 0
    for (xa, ya, va), (xb, yb, vb) in zip(a.points, b.points, strict=True):
        if va == 0 and vb == 0:
            continue
        counted += 1
        if va > 0 and vb > 0:
            total += math.exp(-((xa - xb) ** 2 + (ya - yb) ** 2) / denominator)
    return total / counted if counted else 0.0


def shape_similarity(a: ShapeBase, b: ShapeBase, category: ShapeCategory) -> float:
    """The matching score for a same-category, same-class shape pair.

    `box`/`mask`: IoU in `[0, 1]`. `point`: `1.0` within `POINT_TOLERANCE_PX`,
    else `0.0`. `relation`: always `0.0` (relations are matched separately, by
    identity of their already-matched ends, not by this function).
    """
    if category == "box":
        return envelope_iou(a, b)
    if category == "mask" and isinstance(a, MaskShape) and isinstance(b, MaskShape):
        return mask_iou(a.rle, b.rle)
    if category == "point" and isinstance(a, PointShape) and isinstance(b, PointShape):
        return 1.0 if point_distance(a, b) <= POINT_TOLERANCE_PX else 0.0
    if category == "keypoints" and isinstance(a, KeypointsShape) and isinstance(b, KeypointsShape):
        return keypoint_similarity(a, b)
    if category == "interval" and isinstance(a, SegmentShape) and isinstance(b, SegmentShape):
        return interval_iou(a, b)
    return 0.0


def interval_iou(a: SegmentShape, b: SegmentShape) -> float:
    """Temporal IoU of two segments; 0 when they cover different channels."""
    if set(a.channels or []) != set(b.channels or []):
        return 0.0
    overlap = min(a.end, b.end) - max(a.start, b.start)
    if overlap <= 0:
        return 0.0
    union = max(a.end, b.end) - min(a.start, b.start)
    return overlap / union


@dataclass(slots=True)
class ShapeMatchResult:
    """Tally of a greedy shape match between two results."""

    matched: int = 0
    total_a: int = 0
    total_b: int = 0
    ious: list[float] = field(default_factory=list)

    def combine(self, other: ShapeMatchResult) -> ShapeMatchResult:
        return ShapeMatchResult(
            matched=self.matched + other.matched,
            total_a=self.total_a + other.total_a,
            total_b=self.total_b + other.total_b,
            ious=[*self.ious, *other.ious],
        )


def match_shapes(a: list[Shape], b: list[Shape], iou_threshold: float = 0.5) -> ShapeMatchResult:
    """Greedy shape matching between two results (QA-2 *Matching shapes*).

    Only shapes of the same `class` and a comparable category match: `box`
    (bbox/rbox/polygon) and `mask` need IoU >= `iou_threshold`, `point` needs
    to be within `POINT_TOLERANCE_PX`. All candidate pairs are matched
    greedily in descending score order, each shape used once. Relations
    match when their class matches and both ends matched to each other's
    counterpart. Video shapes additionally need the same `frame`. Polylines
    and spans are excluded entirely — spans have their own metric
    (`match_spans`), polylines never match.
    """
    a_shapes = [s for s in a if shape_category(s) is not None]
    b_shapes = [s for s in b if shape_category(s) is not None]

    candidates: list[tuple[float, int, int, ShapeCategory]] = []
    for i, sa in enumerate(a_shapes):
        cat = shape_category(sa)
        if cat is None or cat == "relation":
            continue
        for j, sb in enumerate(b_shapes):
            if (
                shape_category(sb) != cat
                or sa.class_ != sb.class_
                or (sa.frame, sa.page) != (sb.frame, sb.page)
            ):
                continue
            score = shape_similarity(sa, sb, cat)
            is_candidate = score > 0 if cat == "point" else score >= iou_threshold
            if is_candidate:
                candidates.append((score, i, j, cat))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_a: set[int] = set()
    used_b: set[int] = set()
    id_to_match: dict[UUID, UUID] = {}
    ious: list[float] = []
    matched = 0
    for score, i, j, cat in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        id_to_match[a_shapes[i].id] = b_shapes[j].id
        matched += 1
        if cat in ("box", "mask", "interval"):
            ious.append(score)

    # A relation's end may be a span (text, pdf): exact span matches count as
    # matched ends here, though the spans themselves are scored by `match_spans`.
    b_by_key: dict[SpanKey, list[SpanShape]] = {}
    for sb in _spans(b):
        b_by_key.setdefault(span_key(sb), []).append(sb)
    for sa in _spans(a):
        same = b_by_key.get(span_key(sa))
        if same:
            id_to_match[sa.id] = same.pop(0).id

    a_relations = [(i, s) for i, s in enumerate(a_shapes) if isinstance(s, RelationShape)]
    b_relations = [(j, s) for j, s in enumerate(b_shapes) if isinstance(s, RelationShape)]
    used_b_relations: set[int] = set()
    for _, ra in a_relations:
        for j, rb in b_relations:
            if j in used_b_relations:
                continue
            if ra.class_ != rb.class_ or (ra.frame, ra.page) != (rb.frame, rb.page):
                continue
            if id_to_match.get(ra.from_) == rb.from_ and id_to_match.get(ra.to) == rb.to:
                used_b_relations.add(j)
                matched += 1
                break

    return ShapeMatchResult(
        matched=matched, total_a=len(a_shapes), total_b=len(b_shapes), ious=ious
    )


def shape_f1(result: ShapeMatchResult) -> float | None:
    """`2 * matched / (|A| + |B|)`, `None` when both sides are empty."""
    denom = result.total_a + result.total_b
    return 2 * result.matched / denom if denom else None


def mean_iou(result: ShapeMatchResult) -> float | None:
    """Mean IoU of matched `box` / `mask` pairs, `None` when there are none."""
    return sum(result.ious) / len(result.ious) if result.ious else None


# --------------------------------------------------------------------------
# Span matching
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SpanMatchResult:
    """Tally of exact and overlap span matching between two results."""

    exact_matched: int = 0
    overlap_matched: int = 0
    total_a: int = 0
    total_b: int = 0

    def combine(self, other: SpanMatchResult) -> SpanMatchResult:
        return SpanMatchResult(
            exact_matched=self.exact_matched + other.exact_matched,
            overlap_matched=self.overlap_matched + other.overlap_matched,
            total_a=self.total_a + other.total_a,
            total_b=self.total_b + other.total_b,
        )


def _spans(shapes: Iterable[Shape]) -> list[SpanShape]:
    return [s for s in shapes if isinstance(s, SpanShape)]


SpanKey = tuple[Any, ...]


def span_key(span: SpanShape) -> SpanKey:
    """The exact-match key of a span (QA-2): `class` and its offsets on a text
    item; `class`, `page` and the sorted set of its boxes rounded to whole
    points on a pdf item (CONTRACTS.md *Agreement*).
    """
    if span.boxes is None:
        return ("text", span.class_, *span.offsets)
    boxes = sorted({tuple(round(c) for c in box) for box in span.boxes})
    return ("pdf", span.class_, span.page, tuple(boxes))


def span_overlap(a: SpanShape, b: SpanShape) -> float:
    """How much two spans of the same class overlap, 0 when they don't: shared
    code points on a text item, the summed intersection area of their boxes on
    the same page of a pdf item.
    """
    if a.class_ != b.class_:
        return 0.0
    if a.boxes is None and b.boxes is None:
        a_start, a_end = a.offsets
        b_start, b_end = b.offsets
        return float(max(0, min(a_end, b_end) - max(a_start, b_start)))
    if a.boxes is None or b.boxes is None or a.page != b.page:
        return 0.0
    area = 0.0
    for ax0, ay0, ax1, ay1 in a.boxes:
        for bx0, by0, bx1, by1 in b.boxes:
            width = min(ax1, bx1) - max(ax0, bx0)
            height = min(ay1, by1) - max(ay0, by0)
            if width > 0 and height > 0:
                area += width * height
    return area


def match_spans(a: list[Shape], b: list[Shape]) -> SpanMatchResult:
    """Exact and overlap span matching between two results (QA-2).

    Exact: same `span_key` (class and offsets, or class, page and boxes on
    a pdf). Overlap: same `class`, overlapping ranges (pdf: intersecting
    boxes on the same page), greedy by `span_overlap`. Both are one-to-one within their own
    pass (a span used by the exact pass may still be used by the overlap
    pass — they are independent counts feeding two different F1s).
    """
    a_spans = _spans(a)
    b_spans = _spans(b)

    used_b_exact: set[int] = set()
    exact_matched = 0
    for sa in a_spans:
        for j, sb in enumerate(b_spans):
            if j in used_b_exact:
                continue
            if span_key(sa) == span_key(sb):
                used_b_exact.add(j)
                exact_matched += 1
                break

    candidates: list[tuple[float, int, int]] = []
    for i, sa in enumerate(a_spans):
        for j, sb in enumerate(b_spans):
            overlap = span_overlap(sa, sb)
            if overlap > 0:
                candidates.append((overlap, i, j))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_a_overlap: set[int] = set()
    used_b_overlap: set[int] = set()
    overlap_matched = 0
    for _, i, j in candidates:
        if i in used_a_overlap or j in used_b_overlap:
            continue
        used_a_overlap.add(i)
        used_b_overlap.add(j)
        overlap_matched += 1

    return SpanMatchResult(
        exact_matched=exact_matched,
        overlap_matched=overlap_matched,
        total_a=len(a_spans),
        total_b=len(b_spans),
    )


def span_f1_exact(result: SpanMatchResult) -> float | None:
    denom = result.total_a + result.total_b
    return 2 * result.exact_matched / denom if denom else None


def span_f1_overlap(result: SpanMatchResult) -> float | None:
    denom = result.total_a + result.total_b
    return 2 * result.overlap_matched / denom if denom else None


# --------------------------------------------------------------------------
# Item / project aggregation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _PairAccumulator:
    classification_pairs: list[tuple[Any, Any]] = field(default_factory=list)
    shape: ShapeMatchResult = field(default_factory=ShapeMatchResult)
    span: SpanMatchResult = field(default_factory=SpanMatchResult)
    items: int = 0


def judgement_units(result: AnnotationResult) -> dict[tuple[str, str], Any]:
    """Every nominal judgement in a result, keyed by (field, unit).

    A classification field is one unit per item. On `llm` items a rating is
    one unit per (class, target), reported as field `rating:<class>`, and a
    ranking one unit per class (`ranking:<class>`, the whole order as the
    value), so LLM evaluation gets the same kappa and alpha as classification.
    """
    units: dict[tuple[str, str], Any] = {
        (field, ""): normalize_value(value) for field, value in result.classification.items()
    }
    for shape in result.shapes:
        if isinstance(shape, RatingShape):
            units[(f"rating:{shape.class_}", shape.target)] = shape.value
        elif isinstance(shape, RankingShape):
            units[(f"ranking:{shape.class_}", "")] = tuple(shape.order)
    return units


def _agreement_core(
    items_versions: list[dict[UUID, AnnotationResult]], iou_threshold: float
) -> tuple[list[ClassificationAgreement], ShapeAgreement, SpanAgreement, list[PairAgreement]]:
    classification_rows: dict[str, list[list[Any]]] = {}
    units_by_item: list[dict[UUID, dict[tuple[str, str], Any]]] = []
    for versions in items_versions:
        per_user = {user: judgement_units(result) for user, result in versions.items()}
        units_by_item.append(per_user)
        keys = {key for units in per_user.values() for key in units}
        for key in sorted(keys):
            row = [units[key] for units in per_user.values() if key in units]
            classification_rows.setdefault(key[0], []).append(row)
    fields = set(classification_rows)

    classification = [
        ClassificationAgreement(
            field=f,
            items=len(classification_rows[f]),
            fleiss_kappa=fleiss_kappa(classification_rows[f]),
            krippendorff_alpha=krippendorff_alpha_nominal(classification_rows[f]),
        )
        for f in sorted(fields)
    ]

    pair_acc: dict[tuple[UUID, UUID], _PairAccumulator] = {}
    for versions, per_user in zip(items_versions, units_by_item, strict=True):
        user_ids = sorted(versions.keys(), key=str)
        for a_id, b_id in combinations(user_ids, 2):
            acc = pair_acc.setdefault((a_id, b_id), _PairAccumulator())
            a_res, b_res = versions[a_id], versions[b_id]
            a_units, b_units = per_user[a_id], per_user[b_id]
            for key in sorted(a_units.keys() & b_units.keys()):
                acc.classification_pairs.append((a_units[key], b_units[key]))
            acc.shape = acc.shape.combine(match_shapes(a_res.shapes, b_res.shapes, iou_threshold))
            acc.span = acc.span.combine(match_spans(a_res.shapes, b_res.shapes))
            acc.items += 1

    pairs: list[PairAgreement] = []
    overall_shape = ShapeMatchResult()
    overall_span = SpanMatchResult()
    for (a_id, b_id), acc in pair_acc.items():
        overall_shape = overall_shape.combine(acc.shape)
        overall_span = overall_span.combine(acc.span)
        pairs.append(
            PairAgreement(
                a=a_id,
                b=b_id,
                items=acc.items,
                cohen_kappa=cohen_kappa(acc.classification_pairs),
                mean_iou=mean_iou(acc.shape),
                shape_f1=shape_f1(acc.shape),
                span_f1_exact=span_f1_exact(acc.span),
                span_f1_overlap=span_f1_overlap(acc.span),
            )
        )

    shapes = ShapeAgreement(
        mean_iou=mean_iou(overall_shape),
        f1=shape_f1(overall_shape),
        iou_threshold=iou_threshold,
        envelope_iou=True,
    )
    spans = SpanAgreement(
        f1_exact=span_f1_exact(overall_span), f1_overlap=span_f1_overlap(overall_span)
    )
    return classification, shapes, spans, pairs


def compute_item_agreement(
    versions: dict[UUID, AnnotationResult], iou_threshold: float = 0.5
) -> ItemAgreement:
    """Agreement over one item's per-annotator consensus results (QA-1, QA-2)."""
    classification, shapes, spans, pairs = _agreement_core([versions], iou_threshold)
    for pair in pairs:
        pair.items = None
    return ItemAgreement(classification=classification, shapes=shapes, spans=spans, pairs=pairs)


def compute_project_agreement(
    items_versions: list[dict[UUID, AnnotationResult]], iou_threshold: float = 0.5
) -> tuple[list[ClassificationAgreement], ShapeAgreement, SpanAgreement, list[PairAgreement], int]:
    """Project-wide agreement pooled over items with >= 2 consensus versions (QA-2).

    Returns the pieces of a `ProjectAgreement`; the caller (which knows about
    users) adds `annotators`.
    """
    classification, shapes, spans, pairs = _agreement_core(items_versions, iou_threshold)
    return classification, shapes, spans, pairs, len(items_versions)


def annotator_item_counts(items_versions: list[dict[UUID, AnnotationResult]]) -> dict[UUID, int]:
    """How many pooled items each annotator contributed a version to."""
    counts: Counter[UUID] = Counter()
    for versions in items_versions:
        counts.update(versions.keys())
    return dict(counts)


# --------------------------------------------------------------------------
# Gold accuracy (QA-4)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GoldScore:
    """One gold attempt scored against its reference."""

    classification_accuracy: float | None
    shape_f1: float | None
    mean_iou: float | None
    span_f1: float | None


def classification_accuracy(attempt: dict[str, Any], reference: dict[str, Any]) -> float | None:
    """Matching fields / reference fields; `None` when the reference has none."""
    if not reference:
        return None
    matches = sum(
        1
        for key, value in reference.items()
        if normalize_value(attempt.get(key)) == normalize_value(value)
    )
    return matches / len(reference)


def gold_score(
    attempt: AnnotationResult, reference: AnnotationResult, iou_threshold: float = 0.5
) -> GoldScore:
    """Score one gold attempt against its reference (QA-4): the same functions as agreement."""
    shape_match = match_shapes(attempt.shapes, reference.shapes, iou_threshold)
    span_match = match_spans(attempt.shapes, reference.shapes)
    return GoldScore(
        classification_accuracy=classification_accuracy(
            attempt.classification, reference.classification
        ),
        shape_f1=shape_f1(shape_match),
        mean_iou=mean_iou(shape_match),
        span_f1=span_f1_exact(span_match),
    )
