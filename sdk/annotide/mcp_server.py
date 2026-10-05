"""MCP server for AI agents (API-8): `annotide mcp`.

Serves the Model Context Protocol over stdio and calls the platform's REST
API through `Client`, with an API key. Use a service account's key: the
agent can then do exactly what that account's project memberships allow,
and nothing in this module widens it.

The tools cover what an agent pre-labelling a project needs: find projects
and their label schema, look at items (an image comes back as an image),
read annotations, claim and release tasks, and post pre-labels as an
external producer (`POST /items/{id}/prelabels`). An agent cannot submit,
review, delete or export.

Needs the `mcp` extra: `pip install "annotide[mcp]"`.

Configuration (environment):

- `ANNOTIDE_URL`, `ANNOTIDE_API_KEY` — as for every SDK client.
- `ANNOTIDE_MODEL_VERSION_ID` — the model version `create_prelabel` writes
  under unless the call names one. Register an endpoint-less model for the
  agent (Models page, empty endpoint) and add a version to it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from annotide.client import Client
from annotide.errors import AnnotationError

__all__ = ["MAX_MEDIA_BYTES", "MODEL_VERSION_ENV", "build_server"]

MODEL_VERSION_ENV = "ANNOTIDE_MODEL_VERSION_ID"
#: `view_item` refuses larger media; a model's context is not a file share.
MAX_MEDIA_BYTES = 5 * 1024 * 1024
#: The fields an agent needs from an item; signed URLs and paging noise stay out.
_ITEM_FIELDS = ("id", "path", "media_type", "status", "width", "height", "meta")
_IMAGE_FORMATS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp", ".gif": "gif"}
_TEXT_TYPES = {"text"}

INSTRUCTIONS = """\
Tools for the Annotide. Typical loop for pre-labelling:
1. list_projects, then get_label_schema for the project: shapes must use its
   class names and the tools each class allows.
2. claim_task (or list_items with status "new") to pick an item.
3. view_item to see it, get_annotations to see what is already there.
4. create_prelabel with an Annotation result JSON
   ({"schema_version": 1, "media_type": ..., "classification": {...},
   "shapes": [...]}); a bbox is {"id", "type": "bbox", "class", "bbox":
   [x_min, y_min, x_max, y_max]} in image pixels; every shape `id` is a
   UUID you generate (uuid4).
5. release_task when done; a person reviews and submits the pre-label.
"""

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _item_summary(item: Any) -> dict[str, Any]:
    return {key: item.get(key) for key in _ITEM_FIELDS if key in item}


def build_server(client_factory: Callable[[], Client] | None = None) -> MCPServer:
    """The MCP server; `client_factory` lets tests hand in a client."""
    factory = client_factory or Client
    holder: dict[str, Client] = {}

    def client() -> Client:
        # Created on first use, so `annotide mcp` starts (and lists its tools)
        # even before the environment is complete; the first call says why not.
        if "client" not in holder:
            holder["client"] = factory()
        return holder["client"]

    async def call(fn: Callable[[], Any]) -> Any:
        # The SDK is synchronous; keep the stdio loop responsive. Its errors
        # (a 409, a missing key) are answers the agent should read, not crashes.
        try:
            return await anyio.to_thread.run_sync(fn)
        except AnnotationError as exc:
            raise ToolError(str(exc)) from exc

    server = MCPServer(name="annotide", instructions=INSTRUCTIONS)

    @server.tool(annotations=_READ)
    async def list_projects() -> list[dict[str, Any]]:
        """Projects the API key's account is a member of."""
        projects = await call(lambda: list(client().list_projects()))
        return [
            {"id": p["id"], "name": p["name"], "description": p.get("description")}
            for p in projects
        ]

    @server.tool(annotations=_READ)
    async def get_label_schema(project_id: str) -> dict[str, Any]:
        """The project's current label schema: classes, their tools and attributes."""
        versions = await call(lambda: client().list_schema_versions(project_id))
        if not versions:
            raise AnnotationError(f"project {project_id} has no label schema")
        latest = versions[0]
        return {
            "label_schema_version_id": latest["id"],
            "version": latest["version"],
            "definition": latest["definition"],
        }

    @server.tool(annotations=_READ)
    async def list_items(
        project_id: str, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """A project's items, oldest first; `status` filters (new, prelabeled, annotating, …)."""
        limit = max(1, min(limit, 200))

        def fetch() -> list[dict[str, Any]]:
            items = client().list_items(project_id, status=status)
            out: list[dict[str, Any]] = []
            for item in items:
                out.append(_item_summary(item))
                if len(out) >= limit:
                    break
            return out

        result: list[dict[str, Any]] = await call(fetch)
        return result

    @server.tool(annotations=_READ)
    async def get_item(item_id: str) -> dict[str, Any]:
        """One item's metadata and a short-lived signed `media_url`."""
        item = await call(lambda: client().get_item(item_id))
        return {**_item_summary(item), "media_url": item.get("media_url")}

    @server.tool(annotations=_READ)
    async def view_item(item_id: str) -> Image | str:
        """The item's media itself: an image as an image, a text item as its text."""

        def fetch() -> Image | str:
            item = client().get_item(item_id)
            media_type = str(item.get("media_type"))
            if media_type == "image":
                data = client().download_media(item, max_bytes=MAX_MEDIA_BYTES)
                suffix = PurePosixPath(str(item.get("path", ""))).suffix.lower()
                return Image(data=data, format=_IMAGE_FORMATS.get(suffix, "png"))
            if media_type in _TEXT_TYPES:
                return (
                    client()
                    .download_media(item, max_bytes=MAX_MEDIA_BYTES)
                    .decode("utf-8", errors="replace")
                )
            raise AnnotationError(
                f"view_item shows images and text; this item is {media_type}. "
                "Use get_item for its signed URL."
            )

        result: Image | str = await call(fetch)
        return result

    @server.tool(annotations=_READ)
    async def get_annotations(item_id: str, all_versions: bool = False) -> dict[str, Any]:
        """The item's latest primary annotation version, or its whole history."""
        versions = await call(lambda: client().list_annotations(item_id))
        primary = [v for v in versions if v.get("kind", "primary") == "primary"]
        if all_versions:
            return {"versions": versions}
        return {"latest": primary[0] if primary else None, "version_count": len(versions)}

    @server.tool(annotations=_WRITE)
    async def claim_task(project_id: str, task_type: str = "annotate") -> dict[str, Any]:
        """Claim the next open task in the project (it is locked to you until released)."""
        task = await call(lambda: client().claim_task(project_id, task_type=task_type))
        if task is None:
            return {"task": None, "message": "No open task right now."}
        return {"task": task}

    @server.tool(annotations=_WRITE)
    async def release_task(task_id: str) -> dict[str, Any]:
        """Give a claimed task back to the queue."""
        task = await call(lambda: client().release_task(task_id))
        return {"task": task}

    @server.tool(annotations=_WRITE)
    async def create_prelabel(
        item_id: str,
        result: dict[str, Any],
        model_version_id: str | None = None,
        label_schema_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Post a pre-label: a draft a person will review. Never replaces human work."""
        version = model_version_id or os.environ.get(MODEL_VERSION_ENV)
        if not version:
            raise ToolError(f"no model version: pass model_version_id or set {MODEL_VERSION_ENV}")
        annotation = await call(
            lambda: client().create_prelabel(
                item_id,
                model_version_id=version,
                result=result,
                label_schema_version_id=label_schema_version_id,
            )
        )
        return {
            "annotation_id": annotation["id"],
            "version": annotation["version"],
            "status": annotation["status"],
        }

    return server
