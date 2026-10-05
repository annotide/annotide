"""Models for the label schema JSON (TOOL-1, TOOL-2).

Stored in `label_schema_version.definition` and validated on write.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.schemas.common import BaseSchema

_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


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
    #: LLM evaluation criteria (`llm` items).
    RANKING = "ranking"
    RATING = "rating"
    #: Time intervals on `audio` and `timeseries` items.
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


#: A rating scale wider than this is a free number, not a scale.
MAX_SCALE_STEPS = 21


class ScaleDef(BaseSchema):
    """The integer scale of a `rating` class, e.g. 1-5 with named ends (§5 LLM-data)."""

    min: int
    max: int
    #: Names for some or all values, keyed by the value as a string.
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_scale(self) -> ScaleDef:
        if self.min >= self.max:
            raise ValueError("scale min must be below max")
        if self.max - self.min + 1 > MAX_SCALE_STEPS:
            raise ValueError(f"a scale has at most {MAX_SCALE_STEPS} steps")
        for key in self.labels:
            try:
                value = int(key)
            except ValueError:
                raise ValueError(f"scale label key {key!r} is not an integer") from None
            if not self.min <= value <= self.max:
                raise ValueError(f"scale label {key!r} is outside {self.min}-{self.max}")
        return self


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
    #: Required with the `rating` tool, forbidden without it.
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

    @model_validator(mode="after")
    def _validate_scale_tool(self) -> ClassDef:
        has_tool = ToolType.RATING in self.tools
        if has_tool and self.scale is None:
            raise ValueError(f"class '{self.name}': the rating tool requires a scale")
        if not has_tool and self.scale is not None:
            raise ValueError(f"class '{self.name}': a scale requires the rating tool")
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
