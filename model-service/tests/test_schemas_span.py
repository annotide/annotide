"""SpanShape anchors: offsets for text, boxes for pdf, never both."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import SpanShape

_BASE = {"id": "00000000-0000-0000-0000-000000000001", "class": "PER"}


def test_text_span_and_pdf_span_validate() -> None:
    SpanShape.model_validate({**_BASE, "start": 0, "end": 3})
    SpanShape.model_validate({**_BASE, "page": 1, "boxes": [[0, 0, 10, 10]]})


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"start": 0},
        {"start": 3, "end": 3},
        {"start": 0, "end": 3, "boxes": [[0, 0, 1, 1]]},
        {"boxes": []},
    ],
)
def test_bad_anchors_are_rejected(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SpanShape.model_validate({**_BASE, **extra})
