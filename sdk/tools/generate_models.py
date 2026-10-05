"""Generate `annotide/models.py` from `docs/openapi.json` (API-3).

    python tools/generate_models.py          # rewrite models.py
    python tools/generate_models.py --check  # exit 1 if models.py is stale

The OpenAPI document itself is written by the backend (`make openapi`);
`backend/tests/test_openapi_dump.py` keeps it in step with the app, and
`--check` in CI keeps this file in step with it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
SCHEMA = SDK.parent / "docs" / "openapi.json"
TARGET = SDK / "annotide" / "models.py"

HEADER = '''"""Wire types of the Annotide API — generated, do not edit.

Regenerate with `make openapi` from the repository root. Every model is a
`TypedDict`: API responses are plain dicts, typed for editors and mypy.
"""

'''


def generate() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "models.py"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(SCHEMA),
                "--input-file-type",
                "openapi",
                "--output",
                str(out),
                "--output-model-type",
                "typing.TypedDict",
                "--target-python-version",
                "3.12",
                "--use-standard-collections",
                "--use-union-operator",
                "--enum-field-as-literal",
                "all",
                "--use-schema-description",
                "--use-double-quotes",
                "--disable-timestamp",
                # Open TypedDicts: a field the API adds later must not break
                # a type check against an older SDK (and mypy lacks PEP 728).
                "--no-use-closed-typed-dict",
                "--formatters",
                "ruff-format",
            ],
            check=True,
        )
        body = out.read_text(encoding="utf-8")
    # Drop the generator's own header comment; ours says where it comes from.
    lines = body.splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    return HEADER + "".join(lines).lstrip("\n")


def main(argv: list[str]) -> int:
    text = generate()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET} is stale: run `make openapi` and commit the result", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
