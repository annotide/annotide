"""The platform's own JSONL export format.

One JSON object per line, each line self-contained (DATA-3): item id and path,
image dimensions, schema version and the full annotation result.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.exporters.base import ExportFile, ExportItem, register_exporter
from app.schemas.label_schema import LabelSchemaDefinition


@register_exporter
class NativeExporter:
    """Exports annotation results as one self-contained JSON object per line."""

    format: ClassVar[str] = "native"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        lines: list[str] = []
        for item in items:
            record: dict[str, Any] = {
                "item_id": str(item.id),
                "path": item.path,
                "width": item.width,
                "height": item.height,
                "schema_version": item.result.schema_version,
                "media_type": item.result.media_type,
                "classification": item.result.classification,
                "shapes": [
                    shape.model_dump(mode="json", by_alias=True) for shape in item.result.shapes
                ],
            }
            lines.append(json.dumps(record, sort_keys=False))

        content = ("\n".join(lines) + "\n") if lines else ""
        yield ExportFile(path="annotations.jsonl", data=content.encode("utf-8"))
