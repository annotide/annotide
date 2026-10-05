"""Write the API's OpenAPI document to `docs/openapi.json` (API-3).

The committed file is what the Python SDK generates its models from, and
`tests/test_openapi_dump.py` fails when it drifts from the app. Regenerate
with `make openapi` (or `python -m app.openapi_dump [path]`).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


def openapi_document() -> dict[str, Any]:
    """The schema as a plain JSON value (FastAPI's dict can hold tuples)."""
    # The schema does not depend on settings, but building the app validates
    # them; placeholders let a dump run without a database or a real secret.
    os.environ.setdefault("APP_DATABASE_URL", "postgresql+asyncpg://openapi@localhost/openapi")
    os.environ.setdefault("APP_SECRET_KEY", "openapi-dump-placeholder")
    from app.main import create_app  # after the placeholders: import reads settings

    document: dict[str, Any] = json.loads(json.dumps(create_app().openapi()))
    return document


def render(document: dict[str, Any]) -> str:
    """Stable text: sorted keys so a regeneration only diffs on real changes."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_PATH
    path.write_text(render(openapi_document()), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
