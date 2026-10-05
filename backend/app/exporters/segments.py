"""Segments exporter: time intervals on audio and time-series items (EXP-5, §5).

`segments.jsonl` holds one line per segment. `segments.rttm` is the NIST
RTTM speaker-diarization file for audio segments that name a speaker, the
format diarization toolkits (pyannote, NeMo, dscore) read. See CONTRACTS.md
*Annotation result JSON* → "Audio and time-series items".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.exporters.base import ExportFile, ExportItem, register_exporter, warnings_file
from app.schemas.annotation import SegmentShape
from app.schemas.item import MediaType
from app.schemas.label_schema import LabelSchemaDefinition

_SEGMENT_MEDIA = (MediaType.AUDIO, MediaType.TIMESERIES)


def _rttm_name(path: str) -> str:
    """RTTM fields are whitespace-separated: a path with spaces would break the line."""
    return "_".join(path.split())


def segment_records(
    items: Iterable[ExportItem],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """JSONL records, RTTM lines and warnings for `items`."""
    records: list[dict[str, Any]] = []
    rttm: list[str] = []
    warnings: list[str] = []
    for item in sorted(items, key=lambda entry: entry.path):
        media = item.result.media_type
        if media not in _SEGMENT_MEDIA:
            warnings.append(f"{item.path}: skipped {media} item, not supported by this format")
            continue
        segments = sorted(
            (s for s in item.result.shapes if isinstance(s, SegmentShape)),
            key=lambda s: (s.start, s.end),
        )
        for segment in segments:
            records.append(
                {
                    "item_id": str(item.id),
                    "path": item.path,
                    "media_type": media.value,
                    "class": segment.class_,
                    "start": segment.start,
                    "end": segment.end,
                    "speaker": segment.speaker,
                    "text": segment.text,
                    "channels": segment.channels,
                    "attributes": segment.attributes,
                }
            )
            if media is MediaType.AUDIO and segment.speaker:
                start = segment.start / 1000
                duration = (segment.end - segment.start) / 1000
                speaker = "_".join(segment.speaker.split())
                rttm.append(
                    f"SPEAKER {_rttm_name(item.path)} 1 {start:.3f} {duration:.3f} "
                    f"<NA> <NA> {speaker} <NA> <NA>"
                )
    return records, rttm, warnings


@register_exporter
class SegmentsExporter:
    """Exports `segment` shapes as JSONL plus an RTTM file for speakers."""

    format: ClassVar[str] = "segments"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        records, rttm, warnings = segment_records(items)
        lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]
        yield ExportFile(
            path="segments.jsonl",
            data=("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
        )
        yield ExportFile(
            path="segments.rttm",
            data=("\n".join(rttm) + ("\n" if rttm else "")).encode("utf-8"),
        )
        extra = warnings_file(warnings)
        if extra is not None:
            yield extra
