"""Audio and time-series segments (§5): result rules, QA-6, scan, export, agreement."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.exporters import ExportItem, get_exporter
from app.exporters.segments import segment_records
from app.schemas.annotation import AnnotationResult, SegmentShape, validate_against_schema
from app.schemas.label_schema import LabelSchemaDefinition
from app.services.agreement import compute_item_agreement, interval_iou
from app.services.fusion import fuse
from app.services.scanning import media_type_for

SCHEMA = LabelSchemaDefinition.model_validate(
    {
        "version": 1,
        "classes": [
            {
                "name": "speech",
                "display_name": "Speech",
                "color": "#2563eb",
                "tools": ["segment"],
                "attributes": [{"name": "noisy", "type": "boolean"}],
            },
            {"name": "car", "display_name": "Car", "color": "#e11d48", "tools": ["bbox"]},
        ],
    }
)


def _segment(start: float, end: float, cls: str = "speech", **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "type": "segment",
        "class": cls,
        "start": start,
        "end": end,
        **extra,
    }


def _result(media: str, *shapes: dict[str, Any]) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": media, "shapes": list(shapes)}
    )


class TestResult:
    def test_audio_segments_with_speaker_and_transcript(self) -> None:
        result = _result(
            "audio",
            _segment(0, 1200, speaker="Anna", text="Hei"),
            _segment(800, 2000, speaker="Ben"),  # overlap is fine
        )
        assert validate_against_schema(result, SCHEMA) == []

    def test_series_segments_with_channels(self) -> None:
        result = _result("timeseries", _segment(1.5, 2.25, channels=["x", "y"], text="spike"))
        assert validate_against_schema(result, SCHEMA) == []

    @pytest.mark.parametrize(
        ("media", "shape", "message"),
        [
            ("audio", _segment(5, 5), "greater than start"),
            ("audio", _segment(-1, 5), "greater than or equal to 0"),
            ("audio", _segment(0.5, 5), "whole milliseconds"),
            ("audio", _segment(0, 5, channels=["x"]), "take no channels"),
            ("timeseries", _segment(0, 5, speaker="A"), "take no speaker"),
            ("timeseries", _segment(0, 5, channels=[]), "non-empty names"),
            ("timeseries", _segment(0, 5, channels=["x", "x"]), "unique"),
            ("image", _segment(0, 5), "only allowed on audio and timeseries"),
            (
                "audio",
                {"id": str(uuid4()), "type": "bbox", "class": "car", "bbox": [0, 0, 1, 1]},
                "audio items take only segments",
            ),
        ],
    )
    def test_rules(self, media: str, shape: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            _result(media, shape)

    def test_qa6_checks_the_segment_tool(self) -> None:
        violations = validate_against_schema(_result("audio", _segment(0, 5, cls="car")), SCHEMA)
        assert violations == [
            next(v for v in violations if "tool 'segment' is not allowed for class 'car'" in v)
        ]


def test_scan_maps_audio_and_series() -> None:
    assert media_type_for("rec/a.ogg") == "audio"
    assert media_type_for("rec/a.M4A") == "audio"
    assert media_type_for("sensors/run.timeseries.csv") == "timeseries"
    assert media_type_for("sheets/table.csv") == "text"


def _item(result: AnnotationResult, path: str) -> ExportItem:
    return ExportItem(id=uuid4(), path=path, width=None, height=None, result=result)


class TestExport:
    def test_jsonl_rttm_and_warnings(self) -> None:
        audio = _result(
            "audio",
            _segment(2500, 4000, speaker="Ben Ng", text="Moi"),
            _segment(0, 1500, speaker="Anna"),
            _segment(5000, 6000, cls="speech"),  # no speaker: not in RTTM
        )
        series = _result("timeseries", _segment(1.5, 2.0, channels=["x"]))
        image = AnnotationResult.model_validate({"schema_version": 1, "media_type": "image"})

        records, rttm, warnings = segment_records(
            [
                _item(series, "b/run.timeseries.csv"),
                _item(audio, "a/call 1.wav"),
                _item(image, "c/cat.jpg"),
            ]
        )

        assert [(r["path"], r["start"]) for r in records] == [
            ("a/call 1.wav", 0),
            ("a/call 1.wav", 2500),
            ("a/call 1.wav", 5000),
            ("b/run.timeseries.csv", 1.5),
        ]
        assert records[1]["text"] == "Moi"
        assert records[3]["channels"] == ["x"]
        assert rttm == [
            "SPEAKER a/call_1.wav 1 0.000 1.500 <NA> <NA> Anna <NA> <NA>",
            "SPEAKER a/call_1.wav 1 2.500 1.500 <NA> <NA> Ben_Ng <NA> <NA>",
        ]
        assert warnings == ["c/cat.jpg: skipped image item, not supported by this format"]

    def test_the_exporter_writes_both_files(self) -> None:
        audio = _result("audio", _segment(0, 1000, speaker="Anna"))
        files = {
            f.path: f.data for f in get_exporter("segments").export([_item(audio, "a.wav")], SCHEMA)
        }
        assert set(files) == {"segments.jsonl", "segments.rttm"}
        assert json.loads(files["segments.jsonl"])["speaker"] == "Anna"
        assert files["segments.rttm"].decode().startswith("SPEAKER a.wav 1 0.000 1.000")


def _shape(start: float, end: float, **extra: Any) -> SegmentShape:
    return SegmentShape.model_validate(_segment(start, end, **extra))


def test_interval_iou() -> None:
    assert interval_iou(_shape(0, 10), _shape(5, 15)) == pytest.approx(5 / 15)
    assert interval_iou(_shape(0, 10), _shape(10, 20)) == 0.0
    assert interval_iou(_shape(0, 10, channels=["x"]), _shape(0, 10, channels=["y"])) == 0.0
    assert interval_iou(_shape(0, 10, channels=["x", "y"]), _shape(0, 10, channels=["y", "x"])) == 1


def test_agreement_matches_segments_by_temporal_iou() -> None:
    a, b = uuid4(), uuid4()
    agreement = compute_item_agreement(
        {
            a: _result("audio", _segment(0, 1000), _segment(5000, 6000)),
            b: _result("audio", _segment(100, 1000), _segment(9000, 9500)),
        }
    )
    # One of two segments each matches: F1 = 0.5.
    assert agreement.shapes.f1 == pytest.approx(0.5)
    [pair] = agreement.pairs
    assert pair.mean_iou == pytest.approx(0.9)


def test_fusion_keeps_the_medoid_segment() -> None:
    results = [
        _result("audio", _segment(0, 1000, speaker="Anna", text="Hei")),
        _result("audio", _segment(0, 1100, speaker="Anna", text="Hei")),
        _result("audio", _segment(50, 1000, speaker="Anna", text="Hei!")),
    ]
    fused, conflicts = fuse(results)
    [segment] = [s for s in fused.shapes if isinstance(s, SegmentShape)]
    assert segment.speaker == "Anna"
    assert (segment.start, segment.end) == (0, 1000)
    assert conflicts == []
