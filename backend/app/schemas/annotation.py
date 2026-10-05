"""Models for the annotation result JSON (DATA-8) and the QA-6 schema validator.

Coordinates are pixels in the original image, origin top-left, x right, y down.
Text spans are Unicode code-point offsets into the item's text, pdf spans
boxes on a page; video shapes carry the frame they sit on (CONTRACTS.md
*Annotation result JSON*).
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.schemas.common import BaseSchema
from app.schemas.item import MediaType
from app.schemas.label_schema import AttributeDef, AttributeType, LabelSchemaDefinition, ToolType

Point = tuple[float, float]


class AnnotationKind(StrEnum):
    """Which line of work a version belongs to (QA-1, QA-4).

    `primary` versions are the item's annotation; `consensus` and `gold` are
    per-annotator attempts that exports, stats and "latest version" ignore.
    """

    PRIMARY = "primary"
    CONSENSUS = "consensus"
    GOLD = "gold"


class ShapeBase(BaseSchema):
    """Fields shared by every annotation shape.

    `class_` is aliased to the JSON key `class` (a Python keyword); both the
    alias and the field name are accepted on input via `populate_by_name`.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    class_: str = Field(alias="class")
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    # Video only (media type `video`): the 0-based frame this shape sits on, the
    # track it belongs to, whether it is a keyframe, and whether the object has
    # left the view from this frame on (CVAT `outside`).
    frame: int | None = Field(default=None, ge=0)
    track_id: UUID | None = None
    keyframe: bool = True
    outside: bool = False
    # PDF only (media type `pdf`): the 1-based page, coordinates in PDF points
    # of that page (1/72 in, top-left origin, y down, page rotation applied).
    page: int | None = Field(default=None, ge=1)

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value


def _check_box(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """`[x_min, y_min, x_max, y_max]` with non-negative coordinates and a positive area."""
    x_min, y_min, x_max, y_max = value
    if x_min < 0 or y_min < 0 or x_max < 0 or y_max < 0:
        raise ValueError("bbox coordinates must be >= 0")
    if x_max <= x_min:
        raise ValueError("bbox x_max must be greater than x_min")
    if y_max <= y_min:
        raise ValueError("bbox y_max must be greater than y_min")
    return value


class BBoxShape(ShapeBase):
    """Axis-aligned bounding box: `bbox` is `[x_min, y_min, x_max, y_max]` in pixels (QA-6)."""

    type: Literal["bbox"] = "bbox"
    bbox: tuple[float, float, float, float]
    #: Informational: the document text inside the box (set by the PDF annotator).
    text: str | None = None

    @field_validator("bbox")
    @classmethod
    def _validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        return _check_box(value)


class RBoxShape(ShapeBase):
    """Rotated box: `center` `[cx, cy]`, `size` `[w, h]` and `angle` in degrees.

    Clockwise-positive because image y points down; `w` runs along the
    rotated x axis. Corners are derived, see `corners()`.
    """

    type: Literal["rbox"] = "rbox"
    center: Point
    size: tuple[float, float]
    angle: float

    @field_validator("size")
    @classmethod
    def _validate_size(cls, value: tuple[float, float]) -> tuple[float, float]:
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("rbox size must be greater than 0")
        return value

    @field_validator("angle")
    @classmethod
    def _validate_angle(cls, value: float) -> float:
        if not -180 < value <= 180:
            raise ValueError("rbox angle must be in (-180, 180] degrees")
        return value

    def corners(self) -> list[Point]:
        """The four corners, starting top-left in the box's own frame, clockwise."""
        cx, cy = self.center
        half_w, half_h = self.size[0] / 2, self.size[1] / 2
        theta = math.radians(self.angle)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        return [
            (cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)
            for dx, dy in (
                (-half_w, -half_h),
                (half_w, -half_h),
                (half_w, half_h),
                (-half_w, half_h),
            )
        ]


class PolygonShape(ShapeBase):
    """Closed region: `points` is an open polygon (first point not repeated at the end)."""

    type: Literal["polygon"] = "polygon"
    points: list[Point]

    @field_validator("points")
    @classmethod
    def _validate_points(cls, value: list[Point]) -> list[Point]:
        if len(value) < 3:
            raise ValueError("polygon requires at least 3 points")
        return value


class PolylineShape(ShapeBase):
    """Open path of at least two points."""

    type: Literal["polyline"] = "polyline"
    points: list[Point]

    @field_validator("points")
    @classmethod
    def _validate_points(cls, value: list[Point]) -> list[Point]:
        if len(value) < 2:
            raise ValueError("polyline requires at least 2 points")
        return value


class PointShape(ShapeBase):
    """A single labelled point."""

    type: Literal["point"] = "point"
    point: Point


Keypoint = tuple[float, float, int]


class KeypointsShape(ShapeBase):
    """One skeleton instance: `[x, y, v]` per point of the class's skeleton, in
    its order. `v` is COCO visibility: 0 not labelled, 1 occluded, 2 visible.
    The count is checked against the class's skeleton in `validate_against_schema`.
    """

    type: Literal["keypoints"] = "keypoints"
    points: list[Keypoint]

    @field_validator("points")
    @classmethod
    def _validate_points(cls, value: list[Keypoint]) -> list[Keypoint]:
        if any(v not in (0, 1, 2) for _, _, v in value):
            raise ValueError("keypoint visibility must be 0, 1 or 2")
        if not any(v > 0 for _, _, v in value):
            raise ValueError("keypoints require at least one labelled point")
        return value

    def labelled(self) -> list[Point]:
        """The `(x, y)` of every labelled (v > 0) point."""
        return [(x, y) for x, y, v in self.points if v > 0]

    def envelope(self) -> tuple[float, float, float, float]:
        """Axis-aligned bounds of the labelled points."""
        xs = [p[0] for p in self.labelled()]
        ys = [p[1] for p in self.labelled()]
        return (min(xs), min(ys), max(xs), max(ys))


class MaskRLE(BaseSchema):
    """Uncompressed run-length encoding of a binary mask (COCO-style: alternating

    background/foreground run lengths, starting with background).
    """

    size: tuple[int, int]  # (height, width)
    counts: list[int]


class MaskShape(ShapeBase):
    """A pixel mask, encoded as run-length encoding."""

    type: Literal["mask"] = "mask"
    rle: MaskRLE


class SpanShape(ShapeBase):
    """An entity span (TOOL).

    On a text item, `[start, end)` in Unicode code points of the item's text.
    On a pdf item, `boxes` on `page` — one per line, in PDF points — and the
    span covers the words whose centres fall inside them (CONTRACTS.md *PDF
    items*). Exactly one of the two anchors is set; which one the media type
    takes is checked by `AnnotationResult`. Spans may overlap or nest. `text`
    is the covered text, informational only — the server cannot check it
    without reading the media.
    """

    type: Literal["span"] = "span"
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    boxes: list[tuple[float, float, float, float]] | None = Field(
        default=None, min_length=1, max_length=256
    )
    text: str | None = None

    @field_validator("boxes")
    @classmethod
    def _validate_boxes(
        cls, value: list[tuple[float, float, float, float]] | None
    ) -> list[tuple[float, float, float, float]] | None:
        for box in value or []:
            _check_box(box)
        return value

    @model_validator(mode="after")
    def _validate_anchor(self) -> SpanShape:
        has_range = self.start is not None or self.end is not None
        if has_range and self.boxes is not None:
            raise ValueError("a span takes either 'start'/'end' or 'boxes', not both")
        if not has_range and self.boxes is None:
            raise ValueError("a span needs 'start'/'end' (text) or 'boxes' (pdf)")
        if has_range:
            if self.start is None or self.end is None:
                raise ValueError("a text span needs both 'start' and 'end'")
            if self.end <= self.start:
                raise ValueError("span end must be greater than start")
        return self

    @property
    def offsets(self) -> tuple[int, int]:
        """`(start, end)` of a text span; a pdf span has none (ValueError)."""
        if self.start is None or self.end is None:
            raise ValueError("a pdf span has no character offsets")
        return self.start, self.end


class RelationShape(ShapeBase):
    """A directed relation between two other shapes of the same result (TOOL).

    `from` / `to` are shape ids; both must exist in the result, differ from
    each other, and not be relations themselves.
    """

    type: Literal["relation"] = "relation"
    from_: UUID = Field(alias="from")
    to: UUID

    @model_validator(mode="after")
    def _validate_ends(self) -> RelationShape:
        if self.from_ == self.to:
            raise ValueError("relation 'from' and 'to' must differ")
        return self


class RankingShape(ShapeBase):
    """Responses of an `llm` item ranked best first (§5 LLM-data); two make a preference."""

    type: Literal["ranking"] = "ranking"
    order: list[str] = Field(min_length=1)

    @field_validator("order")
    @classmethod
    def _validate_order(cls, value: list[str]) -> list[str]:
        if any(not response_id for response_id in value):
            raise ValueError("ranking ids must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("a ranking lists each response once")
        return value


#: `response:<id>`, `message:<0-based index>` or `conversation`.
_RATING_TARGET = r"^(response:.+|message:(0|[1-9][0-9]*)|conversation)$"


class RatingShape(ShapeBase):
    """A score on the class's scale for one response, one turn or the conversation."""

    type: Literal["rating"] = "rating"
    target: str = Field(pattern=_RATING_TARGET)
    value: int


class SegmentShape(ShapeBase):
    """An interval on an `audio` or `timeseries` item's time axis (§5).

    Audio: integer milliseconds, with an optional `speaker` and transcript
    `text`. Time series: positions on the CSV's time axis, optionally limited
    to some `channels`. The media-specific rules are in `AnnotationResult`.
    """

    type: Literal["segment"] = "segment"
    start: float = Field(ge=0)
    end: float
    speaker: str | None = Field(default=None, max_length=200)
    text: str | None = None
    channels: list[str] | None = None

    @model_validator(mode="after")
    def _validate_interval(self) -> SegmentShape:
        if self.end <= self.start:
            raise ValueError("segment end must be greater than start")
        if self.channels is not None:
            if not self.channels or any(not name for name in self.channels):
                raise ValueError("segment channels must be non-empty names")
            if len(set(self.channels)) != len(self.channels):
                raise ValueError("segment channels must be unique")
        return self


Shape = Annotated[
    BBoxShape
    | RBoxShape
    | PolygonShape
    | PolylineShape
    | PointShape
    | MaskShape
    | KeypointsShape
    | SpanShape
    | RelationShape
    | RankingShape
    | RatingShape
    | SegmentShape,
    Field(discriminator="type"),
]


#: Shapes that only make sense on `text` and `pdf` items; never valid on other media types.
_TEXT_ONLY_SHAPES: tuple[type[ShapeBase], ...] = (SpanShape, RelationShape)
#: Shapes that only make sense on `llm` items, the only shapes those take.
_LLM_ONLY_SHAPES: tuple[type[ShapeBase], ...] = (RankingShape, RatingShape)


class AnnotationResult(BaseSchema):
    """Top-level annotation result stored in `annotation.result` (DATA-8)."""

    schema_version: int
    media_type: MediaType
    classification: dict[str, Any] = Field(default_factory=dict)
    shapes: list[Shape] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_media_type_rules(self) -> AnnotationResult:
        """`frame` is required on video shapes and `page` on pdf shapes but
        relations, each forbidden elsewhere, and pdf items take no masks;
        span/relation only belong on `text` and `pdf` items, geometric shapes
        never on text; a text span takes offsets, a pdf span boxes
        (CONTRACTS.md *Annotation result JSON*, *PDF items*). Dangling
        relation ids are a schema violation (QA-6), checked separately in
        `validate_against_schema`.
        """
        is_video = self.media_type is MediaType.VIDEO
        is_text = self.media_type is MediaType.TEXT
        is_pdf = self.media_type is MediaType.PDF
        is_llm = self.media_type is MediaType.LLM
        is_audio = self.media_type is MediaType.AUDIO
        is_series = self.media_type is MediaType.TIMESERIES
        for shape in self.shapes:
            is_segment = isinstance(shape, SegmentShape)
            if is_segment and not (is_audio or is_series):
                raise ValueError(
                    f"shape {shape.id}: 'segment' is only allowed on audio and timeseries items"
                )
            if (is_audio or is_series) and not is_segment:
                raise ValueError(
                    f"shape {shape.id}: {self.media_type.value} items take only segments"
                )
            if isinstance(shape, SegmentShape) and is_audio:
                if not (float(shape.start).is_integer() and float(shape.end).is_integer()):
                    raise ValueError(f"shape {shape.id}: audio segments are whole milliseconds")
                if shape.channels is not None:
                    raise ValueError(f"shape {shape.id}: audio segments take no channels")
            if isinstance(shape, SegmentShape) and is_series and shape.speaker is not None:
                raise ValueError(f"shape {shape.id}: time-series segments take no speaker")
            is_llm_shape = isinstance(shape, _LLM_ONLY_SHAPES)
            if is_llm_shape and not is_llm:
                raise ValueError(f"shape {shape.id}: '{shape.type}' is only allowed on llm items")
            if is_llm and not is_llm_shape:
                raise ValueError(f"shape {shape.id}: llm items take only ranking and rating shapes")
            is_relation = isinstance(shape, RelationShape)
            if is_pdf and not is_relation and shape.page is None:
                raise ValueError(f"shape {shape.id}: 'page' is required on pdf items")
            if is_relation and shape.page is not None:
                raise ValueError(f"shape {shape.id}: a relation takes no 'page'")
            if not is_pdf and shape.page is not None:
                raise ValueError(f"shape {shape.id}: 'page' is only allowed on pdf items")
            if isinstance(shape, SpanShape) and is_pdf and shape.boxes is None:
                raise ValueError(f"shape {shape.id}: a pdf span needs 'boxes'")
            if isinstance(shape, SpanShape) and not is_pdf and shape.boxes is not None:
                raise ValueError(f"shape {shape.id}: 'boxes' is only allowed on pdf spans")
            if is_pdf and isinstance(shape, MaskShape):
                raise ValueError(f"shape {shape.id}: 'mask' is not allowed on pdf items")
            if is_video and shape.frame is None:
                raise ValueError(f"shape {shape.id}: 'frame' is required on video items")
            if not is_video and shape.frame is not None:
                raise ValueError(f"shape {shape.id}: 'frame' is only allowed on video items")
            is_text_only_shape = isinstance(shape, _TEXT_ONLY_SHAPES)
            if is_text_only_shape and not (is_text or is_pdf):
                raise ValueError(
                    f"shape {shape.id}: '{shape.type}' is only allowed on text and pdf items"
                )
            if is_text and not is_text_only_shape:
                raise ValueError(
                    f"shape {shape.id}: geometric shapes are not allowed on text items"
                )
        return self


_SHAPE_TOOL: dict[type[ShapeBase], ToolType] = {
    BBoxShape: ToolType.BBOX,
    RBoxShape: ToolType.RBOX,
    PolygonShape: ToolType.POLYGON,
    PolylineShape: ToolType.POLYLINE,
    PointShape: ToolType.POINT,
    MaskShape: ToolType.MASK,
    KeypointsShape: ToolType.KEYPOINTS,
    SpanShape: ToolType.SPAN,
    RelationShape: ToolType.RELATION,
    RankingShape: ToolType.RANKING,
    RatingShape: ToolType.RATING,
    SegmentShape: ToolType.SEGMENT,
}


def tool_for_shape(shape: ShapeBase) -> ToolType:
    """The label-schema tool a shape type corresponds to."""
    return _SHAPE_TOOL[type(shape)]


def _validate_attribute_value(
    name: str, attribute: AttributeDef, value: Any, *, context: str
) -> list[str]:
    """Check a single attribute value against its definition; return violation messages."""
    if attribute.type is AttributeType.BOOLEAN:
        if not isinstance(value, bool):
            return [f"{context}: attribute '{name}' must be a boolean"]
    elif attribute.type is AttributeType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{context}: attribute '{name}' must be a number"]
    elif attribute.type is AttributeType.TEXT:
        if not isinstance(value, str):
            return [f"{context}: attribute '{name}' must be text"]
    elif attribute.type is AttributeType.SELECT:
        if value not in (attribute.options or []):
            return [f"{context}: attribute '{name}' value {value!r} is not in options"]
    elif attribute.type is AttributeType.MULTISELECT:
        options = attribute.options or []
        if not isinstance(value, list) or any(v not in options for v in value):
            return [f"{context}: attribute '{name}' values {value!r} are not in options"]
    return []


def validate_against_schema(
    result: AnnotationResult, definition: LabelSchemaDefinition
) -> list[str]:
    """Validate an annotation result against its label schema (QA-6 pre-submit check).

    Checks: unknown class, tool not allowed for that class, missing required
    attribute, attribute value not in `options`, wrong attribute type — for both
    per-shape attributes and top-level `classification` values.

    Returns a list of human-readable violation messages; an empty list means the
    result is valid.
    """
    violations: list[str] = []
    classes = {cls.name: cls for cls in definition.classes}
    classification_defs = {attr.name: attr for attr in definition.classification}

    for key, value in result.classification.items():
        attribute = classification_defs.get(key)
        if attribute is None:
            violations.append(f"unknown classification attribute '{key}'")
            continue
        violations.extend(
            _validate_attribute_value(key, attribute, value, context="classification")
        )
    for name, attribute in classification_defs.items():
        if attribute.required and name not in result.classification:
            violations.append(f"missing required classification attribute '{name}'")

    for shape in result.shapes:
        context = f"shape {shape.id}"
        class_def = classes.get(shape.class_)
        if class_def is None:
            violations.append(f"{context}: unknown class '{shape.class_}'")
            continue

        required_tool = tool_for_shape(shape)
        if required_tool not in class_def.tools:
            violations.append(
                f"{context}: tool '{required_tool}' is not allowed for class '{shape.class_}'"
            )
        elif isinstance(shape, RatingShape) and class_def.scale is not None:
            scale = class_def.scale
            if not scale.min <= shape.value <= scale.max:
                violations.append(
                    f"{context}: rating {shape.value} is outside {scale.min}-{scale.max} "
                    f"for class '{shape.class_}'"
                )
        elif isinstance(shape, KeypointsShape) and class_def.skeleton is not None:
            expected = len(class_def.skeleton.points)
            if len(shape.points) != expected:
                violations.append(
                    f"{context}: {len(shape.points)} keypoints, class '{shape.class_}' "
                    f"has {expected} in its skeleton"
                )

        attribute_defs = {attr.name: attr for attr in class_def.attributes}
        for attr_name, attr_value in shape.attributes.items():
            attribute = attribute_defs.get(attr_name)
            if attribute is None:
                violations.append(
                    f"{context}: unknown attribute '{attr_name}' for class '{shape.class_}'"
                )
                continue
            violations.extend(
                _validate_attribute_value(attr_name, attribute, attr_value, context=context)
            )
        for attr_name, attribute in attribute_defs.items():
            if attribute.required and attr_name not in shape.attributes:
                violations.append(f"{context}: missing required attribute '{attr_name}'")

    violations.extend(_validate_relations(result))
    violations.extend(_validate_llm_uniqueness(result))
    return violations


def _validate_llm_uniqueness(result: AnnotationResult) -> list[str]:
    """One ranking per class and one rating per (class, target) on an `llm` item."""
    violations: list[str] = []
    rankings: set[str] = set()
    ratings: set[tuple[str, str]] = set()
    for shape in result.shapes:
        if isinstance(shape, RankingShape):
            if shape.class_ in rankings:
                violations.append(f"shape {shape.id}: a second ranking for '{shape.class_}'")
            rankings.add(shape.class_)
        elif isinstance(shape, RatingShape):
            key = (shape.class_, shape.target)
            if key in ratings:
                violations.append(
                    f"shape {shape.id}: a second '{shape.class_}' rating for {shape.target}"
                )
            ratings.add(key)
    return violations


def _validate_relations(result: AnnotationResult) -> list[str]:
    """Dangling `relation` `from`/`to` ids are a schema violation, not a pydantic error (QA-6)."""
    violations: list[str] = []
    non_relation_ids = {shape.id for shape in result.shapes if not isinstance(shape, RelationShape)}
    for shape in result.shapes:
        if not isinstance(shape, RelationShape):
            continue
        context = f"shape {shape.id}"
        if shape.from_ not in non_relation_ids:
            violations.append(f"{context}: relation 'from' id {shape.from_} does not exist")
        if shape.to not in non_relation_ids:
            violations.append(f"{context}: relation 'to' id {shape.to} does not exist")
    return violations
