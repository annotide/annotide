"""Fuse consensus annotation versions into one result (QA-3): pure, no I/O.

`fuse` clusters shapes / spans / relations across N per-annotator results,
keeps clusters with at least `min_votes` members, and reports what it dropped
as `conflicts` — dotted paths like `classification.<field>`, `shape.<class>`,
`span.<class>` or `relation.<class>`. See CONTRACTS.md *Quality control*.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

from app.schemas.annotation import (
    AnnotationResult,
    BBoxShape,
    Keypoint,
    KeypointsShape,
    PointShape,
    RankingShape,
    RatingShape,
    RelationShape,
    Shape,
    ShapeBase,
    SpanShape,
)
from app.services.agreement import (
    ShapeCategory,
    SpanKey,
    normalize_value,
    shape_category,
    shape_similarity,
    span_key,
)


def _default_min_votes(n: int) -> int:
    """Ceil(N/2): a half-vote is kept with an even N (CONTRACTS.md QA-3)."""
    return math.ceil(n / 2)


@dataclass(slots=True)
class _Cluster:
    #: (source result index, shape) — at most one member per source result.
    members: list[tuple[int, ShapeBase]] = field(default_factory=list)


def _fusable_shapes(result: AnnotationResult) -> list[ShapeBase]:
    """Shapes eligible for geometric clustering: not span, not relation."""
    return [
        s
        for s in result.shapes
        if shape_category(s) is not None and shape_category(s) != "relation"
    ]


def _build_clusters(results: list[AnnotationResult], iou_threshold: float) -> list[_Cluster]:
    """Greedy multi-way clustering: each shape joins its best-matching open cluster."""
    clusters: list[_Cluster] = []
    for r_idx, result in enumerate(results):
        for shape in _fusable_shapes(result):
            cat = shape_category(shape)
            assert cat is not None  # _fusable_shapes already filtered out None
            best_cluster: _Cluster | None = None
            best_score = -1.0
            for cluster in clusters:
                if any(idx == r_idx for idx, _ in cluster.members):
                    continue  # at most one shape per annotator per cluster
                rep = cluster.members[0][1]
                if (
                    shape_category(rep) != cat
                    or rep.class_ != shape.class_
                    or rep.frame != shape.frame
                    or rep.page != shape.page
                ):
                    continue
                score = shape_similarity(rep, shape, cat)
                is_candidate = score > 0 if cat == "point" else score >= iou_threshold
                if is_candidate and score > best_score:
                    best_score = score
                    best_cluster = cluster
            if best_cluster is not None:
                best_cluster.members.append((r_idx, shape))
            else:
                clusters.append(_Cluster(members=[(r_idx, shape)]))
    return clusters


def _fuse_attributes(shapes: list[ShapeBase]) -> dict[str, Any]:
    """Per-key majority attribute value; a tie leaves the key unset."""
    keys: set[str] = set()
    for shape in shapes:
        keys.update(shape.attributes.keys())
    fused: dict[str, Any] = {}
    for key in keys:
        values = [normalize_value(s.attributes[key]) for s in shapes if key in s.attributes]
        if not values:
            continue
        counts = Counter(values)
        winner, top = counts.most_common(1)[0]
        tied = [v for v, c in counts.items() if c == top]
        if len(tied) > 1:
            continue
        fused[key] = list(winner) if isinstance(winner, tuple) else winner
    return fused


def _fuse_keypoints(shapes: list[KeypointsShape]) -> list[Keypoint] | None:
    """Per point: labelled when at least half the members label it, at the mean
    of their positions; visible (2) when at least half of those say visible,
    else occluded (1). `None` when the members disagree on the point count or
    no point is left labelled (the caller falls back to the medoid).
    """
    count = len(shapes[0].points)
    if any(len(s.points) != count for s in shapes):
        return None
    fused: list[Keypoint] = []
    for index in range(count):
        labelled = [s.points[index] for s in shapes if s.points[index][2] > 0]
        if not labelled or 2 * len(labelled) < len(shapes):
            fused.append((0.0, 0.0, 0))
            continue
        visible = sum(1 for _, _, v in labelled if v == 2)
        fused.append(
            (
                sum(p[0] for p in labelled) / len(labelled),
                sum(p[1] for p in labelled) / len(labelled),
                2 if 2 * visible >= len(labelled) else 1,
            )
        )
    return fused if any(v > 0 for _, _, v in fused) else None


def _fuse_cluster(members: list[tuple[int, ShapeBase]], category: ShapeCategory) -> ShapeBase:
    """Fuse one cluster's members into a single new shape (QA-3)."""
    shapes = [s for _, s in members]
    cls = shapes[0].class_
    frame = shapes[0].frame
    attributes = _fuse_attributes(shapes)
    track_id = shapes[0].track_id if all(s.track_id == shapes[0].track_id for s in shapes) else None
    new_id = uuid4()

    if category == "box" and all(isinstance(s, BBoxShape) for s in shapes):
        bboxes = [s.bbox for s in shapes if isinstance(s, BBoxShape)]
        confidences = [s.confidence for s in shapes if isinstance(s, BBoxShape)]
        if confidences and all(c is not None for c in confidences):
            weights = [float(c) for c in confidences if c is not None]
            total_weight = sum(weights)
            coords = [
                sum(box[i] * w for box, w in zip(bboxes, weights, strict=True)) / total_weight
                for i in range(4)
            ]
        else:
            coords = [sum(box[i] for box in bboxes) / len(bboxes) for i in range(4)]
        return BBoxShape(
            id=new_id,
            class_=cls,
            bbox=(coords[0], coords[1], coords[2], coords[3]),
            attributes=attributes,
            confidence=None,
            frame=frame,
            page=shapes[0].page,
            track_id=track_id,
            keyframe=shapes[0].keyframe,
            outside=shapes[0].outside,
        )

    if category == "point" and all(isinstance(s, PointShape) for s in shapes):
        points = [s.point for s in shapes if isinstance(s, PointShape)]
        mean_point = (
            sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points),
        )
        return PointShape(
            id=new_id,
            class_=cls,
            point=mean_point,
            attributes=attributes,
            confidence=None,
            frame=frame,
            page=shapes[0].page,
            track_id=track_id,
            keyframe=shapes[0].keyframe,
            outside=shapes[0].outside,
        )

    if category == "keypoints" and all(isinstance(s, KeypointsShape) for s in shapes):
        fused_points = _fuse_keypoints([s for s in shapes if isinstance(s, KeypointsShape)])
        if fused_points is not None:
            return KeypointsShape(
                id=new_id,
                class_=cls,
                points=fused_points,
                attributes=attributes,
                confidence=None,
                frame=frame,
                page=shapes[0].page,
                track_id=track_id,
                keyframe=shapes[0].keyframe,
                outside=shapes[0].outside,
            )

    # rbox / polygon / mask: medoid, the member with the highest mean
    # similarity to the rest of the cluster.
    best_shape = shapes[0]
    best_score = -1.0
    for candidate in shapes:
        others = [s for s in shapes if s is not candidate]
        score = (
            sum(shape_similarity(candidate, other, category) for other in others) / len(others)
            if others
            else 0.0
        )
        if score > best_score:
            best_score = score
            best_shape = candidate
    return best_shape.model_copy(
        update={"id": new_id, "attributes": attributes, "confidence": None, "track_id": track_id}
    )


def _fuse_classification(
    results: list[AnnotationResult], min_votes: int, conflicts: list[str]
) -> dict[str, Any]:
    fields: set[str] = set()
    for result in results:
        fields.update(result.classification.keys())

    fused: dict[str, Any] = {}
    for field_name in sorted(fields):
        values = [
            normalize_value(result.classification[field_name])
            for result in results
            if field_name in result.classification
        ]
        if not values:
            continue
        counts = Counter(values)
        winner, top = counts.most_common(1)[0]
        tied = [v for v, c in counts.items() if c == top]
        if top < min_votes or len(tied) > 1:
            conflicts.append(f"classification.{field_name}")
            continue
        fused[field_name] = list(winner) if isinstance(winner, tuple) else winner
    return fused


def _fuse_llm(
    results: list[AnnotationResult], min_votes: int, conflicts: list[str]
) -> list[ShapeBase]:
    """Rankings (per class) and ratings (per class and target) by the classification rule.

    The most common value wins with at least `min_votes` and no tie; the
    first annotator's shape carries it, so its id stays stable.
    """
    votes: dict[tuple[str, str, str], list[tuple[Any, ShapeBase]]] = {}
    for result in results:
        for shape in result.shapes:
            if isinstance(shape, RankingShape):
                votes.setdefault(("ranking", shape.class_, ""), []).append(
                    (tuple(shape.order), shape)
                )
            elif isinstance(shape, RatingShape):
                votes.setdefault(("rating", shape.class_, shape.target), []).append(
                    (shape.value, shape)
                )
    fused: list[ShapeBase] = []
    for (kind, class_name, target), cast_votes in sorted(votes.items()):
        counts = Counter(value for value, _ in cast_votes)
        winner, top = counts.most_common(1)[0]
        tied = [v for v, c in counts.items() if c == top]
        if top < min_votes or len(tied) > 1:
            conflicts.append(f"{kind}.{class_name}" + (f".{target}" if target else ""))
            continue
        representative = next(shape for value, shape in cast_votes if value == winner)
        fused.append(representative)
    return fused


def _fuse_shapes(
    results: list[AnnotationResult], iou_threshold: float, min_votes: int, conflicts: list[str]
) -> tuple[list[ShapeBase], dict[UUID, UUID]]:
    clusters = _build_clusters(results, iou_threshold)
    fused_shapes: list[ShapeBase] = []
    id_map: dict[UUID, UUID] = {}
    for cluster in clusters:
        if len(cluster.members) < min_votes:
            conflicts.append(f"shape.{cluster.members[0][1].class_}")
            continue
        category = shape_category(cluster.members[0][1])
        assert category is not None  # clustered shapes always have a category
        fused = _fuse_cluster(cluster.members, category)
        for _, shape in cluster.members:
            id_map[shape.id] = fused.id
        fused_shapes.append(fused)
    return fused_shapes, id_map


def _fuse_relations(
    results: list[AnnotationResult], id_map: dict[UUID, UUID], min_votes: int, conflicts: list[str]
) -> list[RelationShape]:
    counts: Counter[tuple[str, UUID, UUID]] = Counter()
    representative: dict[tuple[str, UUID, UUID], RelationShape] = {}
    for result in results:
        seen: set[tuple[str, UUID, UUID]] = set()
        for shape in result.shapes:
            if not isinstance(shape, RelationShape):
                continue
            from_fused = id_map.get(shape.from_)
            to_fused = id_map.get(shape.to)
            if from_fused is None or to_fused is None:
                continue  # an end that did not survive fusion carries no vote
            key = (shape.class_, from_fused, to_fused)
            if key in seen:
                continue  # one vote per annotator per relation
            seen.add(key)
            counts[key] += 1
            representative.setdefault(key, shape)

    fused: list[RelationShape] = []
    for key, count in counts.items():
        cls, from_fused, to_fused = key
        if count < min_votes:
            conflicts.append(f"relation.{cls}")
            continue
        rep = representative[key]
        fused.append(
            RelationShape(
                id=uuid4(),
                class_=cls,
                from_=from_fused,
                to=to_fused,
                attributes=rep.attributes,
                confidence=None,
                frame=rep.frame,
                page=rep.page,
            )
        )
    return fused


def _fuse_spans(
    results: list[AnnotationResult],
    min_votes: int,
    conflicts: list[str],
    id_map: dict[UUID, UUID],
) -> list[SpanShape]:
    """Exact-match span clusters with >= `min_votes` members (QA-3).

    Records source span id → fused span id in `id_map`, so relations between
    spans can find their fused ends.
    """
    counts: Counter[SpanKey] = Counter()
    representative: dict[SpanKey, SpanShape] = {}
    for result in results:
        seen: set[SpanKey] = set()
        for shape in result.shapes:
            if not isinstance(shape, SpanShape):
                continue
            key = span_key(shape)
            if key in seen:
                continue  # one vote per annotator per exact span
            seen.add(key)
            counts[key] += 1
            representative.setdefault(key, shape)

    fused: list[SpanShape] = []
    for key, count in counts.items():
        rep = representative[key]
        if count < min_votes:
            conflicts.append(f"span.{rep.class_}")
            continue
        fused_id = uuid4()
        for result in results:
            for shape in result.shapes:
                if isinstance(shape, SpanShape) and span_key(shape) == key:
                    id_map[shape.id] = fused_id
        fused.append(
            SpanShape(
                id=fused_id,
                class_=rep.class_,
                start=rep.start,
                end=rep.end,
                page=rep.page,
                boxes=rep.boxes,
                text=rep.text,
                attributes=_fuse_attributes(
                    [
                        s
                        for r in results
                        for s in r.shapes
                        if isinstance(s, SpanShape) and span_key(s) == key
                    ]
                ),
                confidence=None,
            )
        )
    return fused


def fuse(
    results: list[AnnotationResult], iou_threshold: float = 0.5, min_votes: int | None = None
) -> tuple[AnnotationResult, list[str]]:
    """Fuse N consensus versions into one result (QA-3).

    `min_votes` (1..N) defaults to `ceil(N/2)`. Returns the fused result and a
    list of `conflicts` — dotted paths for classification fields, shapes,
    spans or relations that were dropped for lacking enough votes or ending
    in a tie.
    """
    if not results:
        raise ValueError("fuse requires at least one result")
    n = len(results)
    votes = min_votes if min_votes is not None else _default_min_votes(n)
    votes = max(1, min(votes, n))

    conflicts: list[str] = []
    classification = _fuse_classification(results, votes, conflicts)
    fused_shapes, id_map = _fuse_shapes(results, iou_threshold, votes, conflicts)
    # Spans first: relations resolve their ends through `id_map`.
    fused_spans = _fuse_spans(results, votes, conflicts, id_map)
    fused_relations = _fuse_relations(results, id_map, votes, conflicts)
    fused_llm = _fuse_llm(results, votes, conflicts)

    fused_result = AnnotationResult(
        schema_version=results[0].schema_version,
        media_type=results[0].media_type,
        classification=classification,
        shapes=cast(list[Shape], [*fused_shapes, *fused_relations, *fused_spans, *fused_llm]),
    )
    return fused_result, conflicts
