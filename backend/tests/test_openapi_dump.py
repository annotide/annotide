"""The committed `docs/openapi.json` must match the app (API-3).

The Python SDK's models are generated from that file, so an API change that
forgets `make openapi` fails here instead of shipping a stale SDK.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import openapi_dump


def test_committed_openapi_matches_the_app() -> None:
    path = openapi_dump.DEFAULT_PATH
    if not path.exists():
        # The compose image carries backend/ only; CI runs from a checkout.
        pytest.skip(f"{path} is not available outside a repository checkout")
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == openapi_dump.openapi_document(), (
        "docs/openapi.json is stale: run `make openapi` and commit the result"
    )


def test_main_writes_a_stable_document(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    assert openapi_dump.main([str(target)]) == 0
    first = target.read_text(encoding="utf-8")
    openapi_dump.main([str(target)])
    assert target.read_text(encoding="utf-8") == first
    document = json.loads(first)
    assert document["info"]["title"] == "Annotide API"
    assert "/api/v1/jobs/{job_id}" in document["paths"]
