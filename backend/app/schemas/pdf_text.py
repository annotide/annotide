"""A word of a PDF's text layer, as the text export formats see it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PdfWord:
    """One whitespace-delimited word of a PDF page (CONTRACTS.md *PDF items*).

    `bbox` is `(x_min, y_min, x_max, y_max)` in the page's points, top-left
    origin, y down, `/Rotate` applied — the space pdf shapes use.
    `line_break_before` is true when the whitespace between the previous word
    and this one held a line break (`\\r` or `\\n`); it decides whether the
    export's document text joins them with `\\n` instead of a space.
    """

    page: int
    text: str
    bbox: tuple[float, float, float, float]
    line_break_before: bool = False
