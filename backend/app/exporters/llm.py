"""LLM evaluation exporter: one `annotations.jsonl` line per `llm` item (EXP-5, §5 LLM-data).

Each line carries the conversation, the candidate responses, the item's
rankings and ratings, and the preference pairs every ranking implies —
`{prompt, chosen, rejected}`, the layout reward models and DPO train on.
The worker reads each item's JSON document and hands it in as
`ExportItem.text`; every other media type, and a document that cannot be
read or parsed, is skipped into `warnings.json`. See CONTRACTS.md
*Annotation result JSON* → "LLM evaluation items".
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.exporters.base import ExportFile, ExportItem, register_exporter, warnings_file
from app.schemas.annotation import RankingShape, RatingShape
from app.schemas.item import MediaType
from app.schemas.label_schema import LabelSchemaDefinition


def _document(item: ExportItem) -> dict[str, Any] | None:
    """The item's JSON document, or None when it is missing or malformed."""
    if item.text is None:
        return None
    try:
        document = json.loads(item.text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    messages = document.get("messages", [])
    responses = document.get("responses", [])
    if not isinstance(messages, list) or not isinstance(responses, list):
        return None
    return document


def _target_exists(target: str, responses: dict[str, Any], message_count: int) -> bool:
    kind, _, key = target.partition(":")
    if kind == "response":
        return key in responses
    if kind == "message":
        return key.isdigit() and int(key) < message_count
    return target == "conversation"


def llm_line(item: ExportItem, document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The JSONL record for one item plus a warning per dangling id."""
    messages: list[Any] = document.get("messages", [])
    responses = [r for r in document.get("responses", []) if isinstance(r, dict) and "id" in r]
    by_id = {str(r["id"]): r for r in responses}
    warnings: list[str] = []

    rankings: dict[str, list[str]] = {}
    pairs: list[dict[str, Any]] = []
    ratings: list[dict[str, Any]] = []
    for shape in item.result.shapes:
        if isinstance(shape, RankingShape):
            missing = [rid for rid in shape.order if rid not in by_id]
            if missing:
                warnings.append(
                    f"{item.path}: ranking '{shape.class_}' names unknown responses {missing}"
                )
            order = [rid for rid in shape.order if rid in by_id]
            rankings[shape.class_] = order
            for better, chosen in enumerate(order):
                for rejected in order[better + 1 :]:
                    pairs.append(
                        {
                            "class": shape.class_,
                            "prompt": messages,
                            "chosen": by_id[chosen],
                            "rejected": by_id[rejected],
                        }
                    )
        elif isinstance(shape, RatingShape):
            if not _target_exists(shape.target, by_id, len(messages)):
                warnings.append(
                    f"{item.path}: rating '{shape.class_}' targets unknown {shape.target}"
                )
                continue
            ratings.append(
                {
                    "class": shape.class_,
                    "target": shape.target,
                    "value": shape.value,
                    "attributes": shape.attributes,
                }
            )
    record = {
        "item_id": str(item.id),
        "path": item.path,
        "messages": messages,
        "responses": responses,
        "classification": item.result.classification,
        "rankings": rankings,
        "ratings": ratings,
        "preference_pairs": pairs,
    }
    return record, warnings


def llm_records(items: Iterable[ExportItem]) -> tuple[list[dict[str, Any]], list[str]]:
    """One record per readable `llm` item, plus every warning (skips and dangling ids)."""
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for item in items:
        if item.result.media_type is not MediaType.LLM:
            warnings.append(
                f"{item.path}: skipped {item.result.media_type} item, not supported by this format"
            )
            continue
        document = _document(item)
        if document is None:
            warnings.append(f"{item.path}: skipped, the LLM document could not be read")
            continue
        record, item_warnings = llm_line(item, document)
        warnings.extend(item_warnings)
        records.append(record)
    return records, warnings


@register_exporter
class LlmExporter:
    """Exports `llm` items as evaluation JSONL with derived preference pairs."""

    format: ClassVar[str] = "llm"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        records, warnings = llm_records(items)
        lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]

        yield ExportFile(
            path="annotations.jsonl",
            data=("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
        )
        extra = warnings_file(warnings)
        if extra is not None:
            yield extra
