"""Where a text item's content lives (CONTRACTS.md *PDF text mode*).

An ordinary text item is its own file on its own connector. A PDF taken in
as text (`settings.pdf_mode: text`) keeps the PDF's path, and its text is
the file `meta.pdf_text.path` on the project's result connector, written by
the `extract_text` job. Every reader of text content — `media_url`,
pre-labelling, the spaCy / CoNLL exports — goes through `text_source`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.models.item import Item
from app.models.project import Project


class PdfTextStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


def pdf_text_path(item_id: UUID) -> str:
    """The extracted text's path on the result connector."""
    return f"text/{item_id}.txt"


def pdf_text_meta(item: Item) -> dict[str, Any] | None:
    """`meta.pdf_text` when the item is a PDF in text mode, else None."""
    value = (item.meta or {}).get("pdf_text")
    return value if isinstance(value, dict) else None


@dataclass(frozen=True, slots=True)
class TextSource:
    connector_id: UUID
    path: str


def text_source(item: Item, project: Project) -> TextSource | None:
    """The connector and path holding `item`'s text.

    None for a PDF whose text is not `ready` yet (or failed).
    """
    meta = pdf_text_meta(item)
    if meta is None:
        return TextSource(item.connector_id, item.path)
    if meta.get("status") != PdfTextStatus.READY:
        return None
    # The connector the text was written to: it stays there when the project's
    # result connector changes later. The path is never taken from `meta`.
    try:
        connector_id = UUID(str(meta["connector_id"]))
    except (KeyError, ValueError):
        if project.result_connector_id is None:
            return None
        connector_id = project.result_connector_id
    return TextSource(connector_id, pdf_text_path(item.id))
