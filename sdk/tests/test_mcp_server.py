"""`annotide.mcp_server` (API-8): the tools, against the scripted fake API."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from annotide.mcp_server import MAX_MEDIA_BYTES, MODEL_VERSION_ENV, build_server
from tests.conftest import FakeApi

# anyio comes with `mcp` and brings its own pytest plugin.
pytestmark = pytest.mark.anyio

ITEM = {
    "id": "i1",
    "project_id": "p1",
    "path": "street/cat.jpg",
    "media_type": "image",
    "status": "new",
    "width": 4,
    "height": 3,
    "meta": {},
    "media_url": "https://storage.example/media/cat.jpg?sig=x",
}
RESULT = {
    "schema_version": 1,
    "media_type": "image",
    "classification": {},
    "shapes": [{"id": "s1", "type": "bbox", "class": "car", "bbox": [0, 0, 2, 2]}],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(api: FakeApi) -> MCPServer:
    return build_server(lambda: api.client())


async def _call(server: MCPServer, name: str, **arguments: Any) -> CallToolResult:
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    return result


def _json(result: CallToolResult) -> Any:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        return content.get("result", content)
    [block] = result.content
    assert isinstance(block, TextContent)
    return json.loads(block.text)


async def _error(server: MCPServer, name: str, **arguments: Any) -> str:
    """A failure the agent reads: over the protocol it is an `is_error` result."""
    with pytest.raises(ToolError) as caught:
        await server.call_tool(name, arguments)
    assert type(caught.value) is ToolError  # anticipated, not a crash
    return str(caught.value)


async def test_lists_the_tools_an_agent_needs_and_nothing_more(server: MCPServer) -> None:
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "list_projects",
        "get_label_schema",
        "list_items",
        "get_item",
        "view_item",
        "get_annotations",
        "claim_task",
        "release_task",
        "create_prelabel",
    }


async def test_projects_schema_and_items(api: FakeApi, server: MCPServer) -> None:
    api.json(
        "GET",
        "/api/v1/projects",
        {"items": [{"id": "p1", "name": "Streets", "description": None}], "next_cursor": None},
    )
    api.json(
        "GET",
        "/api/v1/projects/p1/schemas",
        [{"id": "sv2", "version": 2, "definition": {"classes": [{"name": "car"}]}}],
    )
    api.json(
        "GET",
        "/api/v1/projects/p1/items",
        {"items": [ITEM, {**ITEM, "id": "i2"}, {**ITEM, "id": "i3"}], "next_cursor": None},
    )

    assert _json(await _call(server, "list_projects")) == [
        {"id": "p1", "name": "Streets", "description": None}
    ]
    assert _json(await _call(server, "get_label_schema", project_id="p1")) == {
        "label_schema_version_id": "sv2",
        "version": 2,
        "definition": {"classes": [{"name": "car"}]},
    }
    items = _json(await _call(server, "list_items", project_id="p1", status="new", limit=2))
    assert [item["id"] for item in items] == ["i1", "i2"]
    assert "media_url" not in items[0]
    [request] = api.sent("GET", "/api/v1/projects/p1/items")
    assert request.url.params["status"] == "new"


async def test_view_item_returns_the_image(api: FakeApi, server: MCPServer) -> None:
    api.json("GET", "/api/v1/items/i1", ITEM)
    api.on("GET", "/media/cat.jpg", httpx.Response(200, content=b"\xff\xd8jpeg"))

    result = await _call(server, "view_item", item_id="i1")

    [block] = result.content
    assert isinstance(block, ImageContent)
    assert block.mime_type == "image/jpeg"
    assert base64.b64decode(block.data) == b"\xff\xd8jpeg"
    # The storage request carries the signature, never the API key.
    [media] = api.sent("GET", "/media/cat.jpg")
    assert "authorization" not in media.headers


async def test_view_item_refuses_large_media_and_other_types(
    api: FakeApi, server: MCPServer
) -> None:
    api.json("GET", "/api/v1/items/i1", ITEM)
    api.on("GET", "/media/cat.jpg", httpx.Response(200, content=b"x" * (MAX_MEDIA_BYTES + 1)))
    assert "larger than" in await _error(server, "view_item", item_id="i1")

    api.json("GET", "/api/v1/items/i9", {**ITEM, "id": "i9", "media_type": "video"})
    assert "this item is video" in await _error(server, "view_item", item_id="i9")


async def test_view_item_returns_text(api: FakeApi, server: MCPServer) -> None:
    api.json("GET", "/api/v1/items/t1", {**ITEM, "id": "t1", "media_type": "text"})
    api.on("GET", "/media/cat.jpg", httpx.Response(200, content="Hyvää päivää".encode()))

    result = await _call(server, "view_item", item_id="t1")
    [block] = result.content
    assert isinstance(block, TextContent)
    assert block.text == "Hyvää päivää"


async def test_annotations_latest_primary_or_all(api: FakeApi, server: MCPServer) -> None:
    versions = [
        {"id": "a3", "version": 3, "kind": "consensus"},
        {"id": "a2", "version": 2, "kind": "primary"},
        {"id": "a1", "version": 1, "kind": "primary"},
    ]
    api.json("GET", "/api/v1/items/i1/annotations", versions)

    latest = _json(await _call(server, "get_annotations", item_id="i1"))
    assert latest == {"latest": versions[1], "version_count": 3}
    every = _json(await _call(server, "get_annotations", item_id="i1", all_versions=True))
    assert every == {"versions": versions}


async def test_claim_and_release_tasks(api: FakeApi, server: MCPServer) -> None:
    api.on(
        "POST",
        "/api/v1/tasks/next",
        httpx.Response(200, json={"id": "t1", "item_id": "i1"}),
        httpx.Response(204),
    )
    api.json("POST", "/api/v1/tasks/t1/release", {"id": "t1", "status": "open"})

    assert _json(await _call(server, "claim_task", project_id="p1")) == {
        "task": {"id": "t1", "item_id": "i1"}
    }
    assert _json(await _call(server, "claim_task", project_id="p1"))["task"] is None
    claimed = api.sent("POST", "/api/v1/tasks/next")[0]
    assert dict(claimed.url.params) == {"project_id": "p1", "type": "annotate"}
    assert _json(await _call(server, "release_task", task_id="t1")) == {
        "task": {"id": "t1", "status": "open"}
    }


async def test_create_prelabel_uses_the_configured_model_version(
    api: FakeApi, server: MCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.json(
        "POST",
        "/api/v1/items/i1/prelabels",
        {"id": "a1", "version": 1, "status": "draft"},
        status=201,
    )

    monkeypatch.delenv(MODEL_VERSION_ENV, raising=False)
    assert "no model version" in await _error(
        server, "create_prelabel", item_id="i1", result=RESULT
    )

    monkeypatch.setenv(MODEL_VERSION_ENV, "mv1")
    assert _json(await _call(server, "create_prelabel", item_id="i1", result=RESULT)) == {
        "annotation_id": "a1",
        "version": 1,
        "status": "draft",
    }
    [request] = api.sent("POST", "/api/v1/items/i1/prelabels")
    assert json.loads(request.content) == {"model_version_id": "mv1", "result": RESULT}


async def test_api_errors_reach_the_agent_as_tool_errors(api: FakeApi, server: MCPServer) -> None:
    api.json(
        "POST",
        "/api/v1/items/i1/prelabels",
        {"title": "Conflict", "status": 409, "detail": "The item already has a human annotation"},
        status=409,
    )
    message = await _error(
        server, "create_prelabel", item_id="i1", result=RESULT, model_version_id="m"
    )
    assert "human annotation" in message
