"""The reference model service keeps its own copy of the shape schema.

It cannot import the backend (separate package and image), so the copy
drifts: once it lacked the `keypoints`/`rbox` tools and every `/predict`
failed with 422. This compares the two where they must agree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.schemas import annotation as backend
from app.schemas.item import MediaType
from app.schemas.label_schema import ToolType

_PATH = Path(__file__).resolve().parents[2] / "model-service" / "app" / "schemas.py"
#: Video tracks and text relations never come out of the reference model.
_BACKEND_ONLY = {"frame", "track_id", "keyframe", "outside"}


@pytest.fixture(scope="module")
def model_schemas() -> ModuleType:
    if not _PATH.exists():
        pytest.skip("model-service is not checked out next to the backend")
    spec = importlib.util.spec_from_file_location("model_service_schemas", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_enums_match(model_schemas: ModuleType) -> None:
    assert {tool.value for tool in model_schemas.ToolType} == {tool.value for tool in ToolType}
    assert {m.value for m in model_schemas.MediaType} == {m.value for m in MediaType}


@pytest.mark.parametrize(
    "name",
    [
        "BBoxShape",
        "RBoxShape",
        "PolygonShape",
        "PolylineShape",
        "PointShape",
        "KeypointsShape",
        "MaskShape",
        "SpanShape",
    ],
)
def test_shape_fields_match(model_schemas: ModuleType, name: str) -> None:
    ours = set(getattr(backend, name).model_fields) - _BACKEND_ONLY
    theirs = set(getattr(model_schemas, name).model_fields)
    assert theirs == ours


def test_label_schema_fields_are_mirrored(model_schemas: ModuleType) -> None:
    """The platform sends the whole label schema to `/predict`; the model service
    forbids unknown keys, so a field added here and not there breaks every
    pre-labelling job (found by the E2E suite when `scale` was added)."""
    from app.schemas.label_schema import AttributeDef, ClassDef, LabelSchemaDefinition

    for name, ours in (
        ("LabelSchemaDefinition", LabelSchemaDefinition),
        ("ClassDef", ClassDef),
        ("AttributeDef", AttributeDef),
    ):
        theirs = getattr(model_schemas, name)
        missing = set(ours.model_fields) - set(theirs.model_fields)
        assert not missing, f"model-service {name} lacks {sorted(missing)}"
