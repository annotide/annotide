"""CoNLL-format exporter: `annotations.conll`, IOB2 tags per token (EXP-5).

`text` and `pdf` items have a representation; everything else is skipped into
`warnings.json`, the mirror of COCO/YOLO's `split_image_items`. See
CONTRACTS.md *Annotation result JSON* → "Text export formats (EXP-5)".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import ClassVar

from app.exporters.base import (
    ExportFile,
    ExportItem,
    entity_set_for_item,
    prepare_text_item,
    register_exporter,
    split_text_items,
    warnings_file,
)
from app.schemas.annotation import SpanShape
from app.schemas.label_schema import LabelSchemaDefinition

#: `\w+` runs (words/numbers) or a single non-space, non-word character
#: (punctuation) — CONTRACTS.md's CoNLL tokenisation rule.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def _tag_for(position: int, entities: list[SpanShape]) -> str:
    """IOB2 tag for the token whose first character sits at `position`.

    Entities never overlap (they are the entity set), so at most one match.
    """
    for span in entities:
        start, end = span.offsets
        if start <= position < end:
            return f"{'B' if position == start else 'I'}-{span.class_}"
    return "O"


def _lines_for(item: ExportItem, entities: list[SpanShape]) -> list[str]:
    text = item.text or ""
    lines = [f"# item_id = {item.id}", f"# path = {item.path}"]
    for match in _TOKEN_RE.finditer(text):
        lines.append(f"{match.group()}\t{_tag_for(match.start(), entities)}")
    return lines


@register_exporter
class ConllExporter:
    """Exports text items as one `# item_id` / `# path` block plus IOB2 tokens each."""

    format: ClassVar[str] = "conll"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        text_items, warnings = split_text_items(items)

        blocks: list[str] = []
        for raw in text_items:
            item, prepare_warnings = prepare_text_item(raw)
            warnings.extend(prepare_warnings)
            entities, drop_warnings = entity_set_for_item(item)
            warnings.extend(drop_warnings)
            blocks.append("\n".join(_lines_for(item, entities)))

        content = ("\n\n".join(blocks) + "\n\n") if blocks else ""
        yield ExportFile(path="annotations.conll", data=content.encode("utf-8"))
        warnings_export = warnings_file(warnings)
        if warnings_export is not None:
            yield warnings_export
