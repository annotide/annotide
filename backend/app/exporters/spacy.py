"""spaCy-format exporter: one `annotations.jsonl` line per text item (EXP-5).

`text` and `pdf` items have a representation; everything else is skipped into
`warnings.json`, the mirror of COCO/YOLO's `split_image_items`. See
CONTRACTS.md *Annotation result JSON* → "Text export formats (EXP-5)".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.exporters.base import (
    ExportFile,
    ExportItem,
    entity_set_for_item,
    prepare_text_item,
    register_exporter,
    split_text_items,
    warnings_file,
)
from app.schemas.annotation import RelationShape, SpanShape
from app.schemas.item import MediaType
from app.schemas.label_schema import LabelSchemaDefinition


def _spans_and_relations(
    item: ExportItem,
) -> tuple[list[SpanShape], list[list[Any]]]:
    """All of `item`'s spans (text order) plus its relations indexed into them."""
    spans = sorted(
        (shape for shape in item.result.shapes if isinstance(shape, SpanShape)),
        key=lambda span: span.offsets,
    )
    index_by_id = {span.id: index for index, span in enumerate(spans)}
    relations: list[list[Any]] = []
    for shape in item.result.shapes:
        if not isinstance(shape, RelationShape):
            continue
        head = index_by_id.get(shape.from_)
        child = index_by_id.get(shape.to)
        if head is None or child is None:
            continue  # QA-6 rejects dangling relation ids before export
        relations.append([head, child, shape.class_])
    return spans, relations


@register_exporter
class SpacyExporter:
    """Exports text items as spaCy-ready `{"text", "entities", "spans", ...}` JSONL."""

    format: ClassVar[str] = "spacy"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        text_items, warnings = split_text_items(items)

        lines: list[str] = []
        for raw in text_items:
            item, prepare_warnings = prepare_text_item(raw)
            warnings.extend(prepare_warnings)
            entities, drop_warnings = entity_set_for_item(item)
            warnings.extend(drop_warnings)
            spans, relations = _spans_and_relations(item)
            meta = {"item_id": str(item.id), "path": item.path}
            if item.result.media_type is MediaType.PDF:
                meta["media_type"] = "pdf"
            record = {
                "text": item.text or "",
                "entities": [[*span.offsets, span.class_] for span in entities],
                "spans": {"sc": [[*span.offsets, span.class_] for span in spans]},
                "relations": relations,
                "classification": item.result.classification,
                "meta": meta,
            }
            lines.append(json.dumps(record, sort_keys=False))

        content = ("\n".join(lines) + "\n") if lines else ""
        yield ExportFile(path="annotations.jsonl", data=content.encode("utf-8"))
        warnings_export = warnings_file(warnings)
        if warnings_export is not None:
            yield warnings_export
