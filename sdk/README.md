# annotide

Python SDK and command line for the Annotide API (API-3). One
runtime dependency (`httpx`); responses are plain dicts typed with
`TypedDict`s generated from the API's OpenAPI document.

```sh
pip install -e sdk            # from a checkout; not on a package index yet
export ANNOTIDE_URL=https://annotate.example.com
export ANNOTIDE_API_KEY=... # Settings → API keys; use a service account for CI
```

## Python

```python
from annotide import Client, JobFailedError

with Client() as client:  # reads ANNOTIDE_URL / ANNOTIDE_API_KEY
    project = next(p for p in client.list_projects() if p["name"] == "Cars")

    # Freeze the dataset, then export it reproducibly.
    snapshot = client.take_snapshot(
        project["id"],
        "2026-q3",
        split={"train": 0.8, "val": 0.1, "test": 0.1, "seed": 0, "group_by": "folder"},
    )
    client.export(project["id"], "coco", "train.zip", snapshot_id=snapshot["id"], split="train")

    # Import existing labels; a dry run reports what would happen.
    report = client.import_file(project["id"], "labels.json", "coco", dry_run=True)
    print(report["result"])
```

- Errors are `ApiError` (`status`, `title`, `detail` from the RFC 9457
  body); a job that ends `failed`/`cancelled` raises `JobFailedError`, one
  that outlives its `timeout` raises `JobTimeoutError`.
- A 429 waits for `Retry-After` and repeats. Gateway errors (502/503/504)
  repeat only when that is harmless: reads, and creates, which always carry
  an `Idempotency-Key` so a repeat answers the first row.
- Export downloads go straight to storage with the signed URL; the API key
  is never sent there.
- `client.request(method, path, ...)` reaches any endpoint without a method.

## Command line

```sh
annotide whoami
annotide projects list | jq -r '.name'
annotide snapshots create <project> 2026-q3 --split 0.8,0.1,0.1 --group-by folder
annotide export <project> --format coco --snapshot <id> --split train -o train.zip
annotide import <project> labels.json --format coco --class-map '{"auto": "car"}' --dry-run
annotide jobs wait <job>
```

Lists print as JSON Lines, single objects as JSON; errors go to stderr with
exit status 1. The key is read from `ANNOTIDE_API_KEY` only, so it never
lands in shell history.

## MCP server for AI agents (API-8)

`annotide mcp` serves the [Model Context Protocol](https://modelcontextprotocol.io)
over stdio, so an AI agent (Claude Code, Claude Desktop, or any MCP client)
can pre-label a project. It calls the REST API with an API key, so the agent
can do exactly what that account may do. It can list projects and their label
schema, view items (images come back as images), read annotations, claim and
release tasks, and post pre-labels. It cannot submit, review, delete or
export.

Setup, once per agent:

1. **Service account.** Settings → API keys → Service accounts: create one,
   mint a `write` key, and add the account to the project as an annotator.
2. **Model.** Models → Register model, name it after the agent, and leave
   the endpoint empty. That makes it an *external producer*: the platform
   never calls it, and the agent posts the pre-labels. Add version 1 and
   note its id.
3. **Client config.** For Claude Code (`.mcp.json`) or Claude Desktop:

```json
{
  "mcpServers": {
    "annotation": {
      "command": "annotation",
      "args": ["mcp"],
      "env": {
        "ANNOTIDE_URL": "https://annotate.example.com",
        "ANNOTIDE_API_KEY": "ak_…",
        "ANNOTIDE_MODEL_VERSION_ID": "<model version id>"
      }
    }
  }
}
```

Install with `pip install "annotide[mcp]"`. Pre-labels arrive as
`draft` versions authored by the model version. People review and submit
them as usual, and a pre-label never replaces human work (ML-10).

## Development

```sh
make install                  # from the repository root: creates sdk/.venv
cd sdk && .venv/bin/pytest && .venv/bin/mypy annotide tests tools
make openapi                  # after an API change: docs/openapi.json + models.py
```

`annotide/models.py` is generated; never edit it by hand. CI fails when
it or `docs/openapi.json` is stale.
