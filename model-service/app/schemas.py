"""DTOs for the model service.

The `Shape*` / `AnnotationResult` / `LabelSchemaDefinition` family below is a
deliberate copy of `backend/app/schemas/annotation.py` and
`backend/app/schemas/label_schema.py` — same field names, same validation,
same JSON shape (see `docs/CONTRACTS.md`, "Annotation result JSON" and "Label
schema JSON"). It is copied rather than imported so this service can be built,
tested and deployed on its own, independent of the backend package.

Coordinates in `AnnotationResult.shapes` are pixels in the *original* image,
origin top-left, x right, y down. `bbox` is `[x_min, y_min, x_max, y_max]`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

Point = tuple[float, float]
Keypoint = tuple[float, float, int]


class BaseSchema(BaseModel):
    """Base configuration shared by every DTO, mirroring the backend's `BaseSchema`."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


# --------------------------------------------------------------------------
# Label schema JSON (TOOL-1, TOOL-2) — copied from backend/app/schemas/label_schema.py
# --------------------------------------------------------------------------


class AttributeType(StrEnum):
    """Kind of value an attribute holds."""

    TEXT = "text"
    NUMBER = "number"
    SELECT = "select"
    MULTISELECT = "multiselect"
    BOOLEAN = "boolean"


class ToolType(StrEnum):
    """Annotation tool a class can be drawn with."""

    BBOX = "bbox"
    RBOX = "rbox"
    POLYGON = "polygon"
    POLYLINE = "polyline"
    POINT = "point"
    MASK = "mask"
    KEYPOINTS = "keypoints"
    SPAN = "span"
    RELATION = "relation"
    CLASSIFICATION = "classification"
    RANKING = "ranking"
    RATING = "rating"
    SEGMENT = "segment"


class AttributeDef(BaseSchema):
    """One attribute definition, attached to a class or to top-level classification."""

    name: str
    type: AttributeType
    required: bool = False
    default: Any | None = None
    options: list[str] | None = None

    @model_validator(mode="after")
    def _validate_options(self) -> AttributeDef:
        needs_options = self.type in (AttributeType.SELECT, AttributeType.MULTISELECT)
        if needs_options and not self.options:
            raise ValueError(
                f"attribute '{self.name}': type '{self.type}' requires non-empty options"
            )
        if not needs_options and self.options:
            raise ValueError(
                f"attribute '{self.name}': type '{self.type}' must not declare options"
            )
        return self


class SkeletonDef(BaseSchema):
    """Named keypoints of a `keypoints` class and the bones between them (TOOL).

    The order of `points` is the order a `keypoints` shape stores them in.
    `edges` are 0-based index pairs into `points`.
    """

    points: list[str]
    edges: list[tuple[int, int]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_skeleton(self) -> SkeletonDef:
        if not self.points:
            raise ValueError("skeleton must name at least one point")
        if any(not name.strip() for name in self.points):
            raise ValueError("skeleton point names must not be empty")
        duplicates = sorted({n for n in self.points if self.points.count(n) > 1})
        if duplicates:
            raise ValueError(f"skeleton point names must be unique: duplicated {duplicates}")
        seen: set[frozenset[int]] = set()
        for a, b in self.edges:
            if not (0 <= a < len(self.points) and 0 <= b < len(self.points)):
                raise ValueError(f"skeleton edge [{a}, {b}] is out of range")
            if a == b:
                raise ValueError(f"skeleton edge [{a}, {b}] must join two different points")
            pair = frozenset((a, b))
            if pair in seen:
                raise ValueError(f"skeleton edge [{a}, {b}] is duplicated")
            seen.add(pair)
        return self


class ScaleDef(BaseSchema):
    """The integer scale of a `rating` class (LLM evaluation); validated by the platform."""

    min: int
    max: int
    labels: dict[str, str] = Field(default_factory=dict)


class ClassDef(BaseSchema):
    """One annotation class: its display, tools and attributes."""

    name: str
    display_name: str
    color: str
    hotkey: str | None = None
    tools: list[ToolType]
    attributes: list[AttributeDef] = Field(default_factory=list)
    #: Required with the `keypoints` tool, forbidden without it.
    skeleton: SkeletonDef | None = None
    #: With the `rating` tool (LLM evaluation); the platform sends it for every class.
    scale: ScaleDef | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        if not _COLOR_PATTERN.fullmatch(value):
            raise ValueError(f"color '{value}' must match ^#[0-9a-fA-F]{{6}}$")
        return value

    @field_validator("hotkey")
    @classmethod
    def _validate_hotkey(cls, value: str | None) -> str | None:
        if value is not None and len(value) != 1:
            raise ValueError(f"hotkey '{value}' must be a single character")
        return value

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: list[ToolType]) -> list[ToolType]:
        if not value:
            raise ValueError("class must declare at least one tool")
        return value

    @model_validator(mode="after")
    def _validate_skeleton_tool(self) -> ClassDef:
        has_tool = ToolType.KEYPOINTS in self.tools
        if has_tool and self.skeleton is None:
            raise ValueError(f"class '{self.name}': the keypoints tool requires a skeleton")
        if not has_tool and self.skeleton is not None:
            raise ValueError(f"class '{self.name}': a skeleton requires the keypoints tool")
        return self


class LabelSchemaDefinition(BaseSchema):
    """The full label schema stored in `label_schema_version.definition`."""

    version: int
    classes: list[ClassDef]
    classification: list[AttributeDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_class_names(self) -> LabelSchemaDefinition:
        names = [cls.name for cls in self.classes]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"class names must be unique: duplicated {duplicates}")
        return self

    @model_validator(mode="after")
    def _validate_unique_hotkeys(self) -> LabelSchemaDefinition:
        hotkeys = [cls.hotkey for cls in self.classes if cls.hotkey is not None]
        duplicates = sorted({key for key in hotkeys if hotkeys.count(key) > 1})
        if duplicates:
            raise ValueError(f"hotkeys must be unique across classes: duplicated {duplicates}")
        return self


# --------------------------------------------------------------------------
# Annotation result JSON (DATA-8) — copied from backend/app/schemas/annotation.py
# --------------------------------------------------------------------------


class MediaType(StrEnum):
    """Kind of media an item holds (subset relevant to this service)."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    PDF = "pdf"
    LLM = "llm"
    TIMESERIES = "timeseries"


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
    # PDF only: the 1-based page; coordinates are that page's PDF points
    # (top-left origin, y down, `/Rotate` applied).
    page: int | None = Field(default=None, ge=1)

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return value


class BBoxShape(ShapeBase):
    """Axis-aligned bounding box: `bbox` is `[x_min, y_min, x_max, y_max]` in pixels (QA-6)."""

    type: Literal["bbox"] = "bbox"
    bbox: tuple[float, float, float, float]
    #: Informational: the document text inside the box (PDF items).
    text: str | None = None

    @field_validator("bbox")
    @classmethod
    def _validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        x_min, y_min, x_max, y_max = value
        if x_min < 0 or y_min < 0 or x_max < 0 or y_max < 0:
            raise ValueError("bbox coordinates must be >= 0")
        if x_max <= x_min:
            raise ValueError("bbox x_max must be greater than x_min")
        if y_max <= y_min:
            raise ValueError("bbox y_max must be greater than y_min")
        return value


class RBoxShape(ShapeBase):
    """Rotated box: `center` `[cx, cy]`, `size` `[w, h]` and `angle` in degrees, clockwise."""

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


class KeypointsShape(ShapeBase):
    """One skeleton instance: `[x, y, v]` per skeleton point, COCO visibility `v`."""

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


class MaskRLE(BaseSchema):
    """Uncompressed run-length encoding of a binary mask (COCO-style)."""

    size: tuple[int, int]  # (height, width)
    counts: list[int]


class MaskShape(ShapeBase):
    """A pixel mask, encoded as run-length encoding."""

    type: Literal["mask"] = "mask"
    rle: MaskRLE


class SpanShape(ShapeBase):
    """An entity span: on text `[start, end)` in code points; on a pdf item `boxes`
    (one per line, PDF points) on `page`. Exactly one anchor is set.
    """

    type: Literal["span"] = "span"
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    boxes: list[tuple[float, float, float, float]] | None = Field(
        default=None, min_length=1, max_length=256
    )
    text: str | None = None

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


Shape = Annotated[
    BBoxShape
    | RBoxShape
    | PolygonShape
    | PolylineShape
    | PointShape
    | KeypointsShape
    | MaskShape
    | SpanShape,
    Field(discriminator="type"),
]


class AnnotationResult(BaseSchema):
    """Top-level annotation result, matching `annotation.result` (DATA-8)."""

    schema_version: int
    media_type: MediaType
    classification: dict[str, Any] = Field(default_factory=dict)
    shapes: list[Shape] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Model service request/response DTOs (§8 of the requirements)
# --------------------------------------------------------------------------


class HealthResponse(BaseSchema):
    """`GET /health` liveness payload."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseSchema):
    """`GET /ready` readiness payload."""

    status: Literal["ok"] = "ok"
    backend: str


class InfoResponse(BaseSchema):
    """`GET /info` — model identity and capabilities."""

    name: str
    version: str
    task: Literal["detect"] = "detect"
    media_types: list[MediaType]
    classes: list[str]
    gpu: bool
    backend: str
    #: The OCR engine behind `/ocr` (`tesseract`, `anthropic`, `openai`), or null.
    ocr_engine: str | None = None


class PredictItem(BaseSchema):
    """One item to run prediction on.

    `media_type` says how to read `url`: an image, or UTF-8 text (then
    `width` / `height` are 0 and the answer holds `span` shapes).
    """

    id: str
    url: str
    width: int
    height: int
    media_type: MediaType = MediaType.IMAGE


class PredictRequest(BaseSchema):
    """`POST /predict` request body."""

    items: list[PredictItem]
    schema_: LabelSchemaDefinition = Field(alias="schema")
    confidence_threshold: float = 0.0

    model_config = ConfigDict(from_attributes=True, extra="forbid", populate_by_name=True)


class ItemPrediction(BaseSchema):
    """Prediction result for one item: an `AnnotationResult` plus item-level confidence."""

    item_id: str
    result: AnnotationResult
    confidence: float
    error: str | None = None


class PredictResponse(BaseSchema):
    """`POST /predict` response body."""

    predictions: list[ItemPrediction]


class PointPrompt(BaseSchema):
    """A single point prompt, in original image pixels."""

    x: float
    y: float


class BoxPrompt(BaseSchema):
    """A box prompt, `[x_min, y_min, x_max, y_max]` in original image pixels."""

    bbox: tuple[float, float, float, float]


class InteractiveRequest(BaseSchema):
    """`POST /interactive` request body.

    Exactly one of `point`, `box`, or `text` should be given; `point`/`box`
    are the supported prompt kinds for this reference backend.
    """

    item_id: str
    url: str
    width: int
    height: int
    point: PointPrompt | None = None
    box: BoxPrompt | None = None
    text: str | None = None


class InteractiveResponse(BaseSchema):
    """`POST /interactive` response body: a polygon spanning the prompted region."""

    type: Literal["polygon"] = "polygon"
    points: list[Point]
    confidence: float


class EmbedItem(BaseSchema):
    """One item to embed."""

    id: str
    url: str


class EmbedRequest(BaseSchema):
    """`POST /embed` request body."""

    items: list[EmbedItem]


class ItemEmbedding(BaseSchema):
    """Embedding vector for one item."""

    item_id: str
    vector: list[float]
    error: str | None = None


class EmbedResponse(BaseSchema):
    """`POST /embed` response body."""

    dimensions: int
    embeddings: list[ItemEmbedding]


class OcrRequest(BaseSchema):
    """`POST /ocr`: read the words of a scanned PDF's pages (or of an image).

    `pages` are 1-based and only apply to pdf; omitted, the first
    `OCR_MAX_PAGES` pages are read.
    """

    item_id: str
    url: str
    media_type: MediaType = MediaType.PDF
    pages: list[Annotated[int, Field(ge=1)]] | None = None


class OcrWordOut(BaseSchema):
    """One word; `bbox` `[x_min, y_min, x_max, y_max]` in page points (pdf) or pixels."""

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None = None


class OcrPageOut(BaseSchema):
    page: int
    width: float
    height: float
    words: list[OcrWordOut]


class OcrResponse(BaseSchema):
    """`POST /ocr` response: the engine that read the pages, and their words."""

    engine: str
    pages: list[OcrPageOut]
