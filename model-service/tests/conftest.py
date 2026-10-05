"""Shared fixtures: OCR is off unless a test opts in, whatever is installed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.ocr import get_engine


@pytest.fixture(autouse=True)
def _no_ocr_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OCR_ENGINE", "none")
    # The real function: a test may have monkeypatched `ocr.get_engine` itself.
    get_engine.cache_clear()
    yield
    get_engine.cache_clear()
