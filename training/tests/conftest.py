"""A fake platform: the five endpoints the pipeline calls, over httpx.MockTransport."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from annotide import Client

PROJECT = "11111111-1111-1111-1111-111111111111"
SNAPSHOT = "22222222-2222-2222-2222-222222222222"
MODEL = "33333333-3333-3333-3333-333333333333"
DIGEST = "a" * 64


def record(
    item_id: str, boxes: list[tuple[str, list[float]]], w: int = 100, h: int = 100
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "path": f"images/{item_id}.jpg",
        "width": w,
        "height": h,
        "schema_version": 1,
        "media_type": "image",
        "classification": {},
        "shapes": [
            {"id": f"{item_id}-{n}", "type": "bbox", "class": c, "attributes": {}, "bbox": b}
            for n, (c, b) in enumerate(boxes)
        ],
    }


def export_zip(
    splits: dict[str, list[dict[str, Any]]] | None = None,
    flat: list[dict[str, Any]] | None = None,
    *,
    snapshot_id: str = SNAPSHOT,
    digest: str = DIGEST,
    export_format: str = "native",
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps({"format": export_format, "snapshot_id": snapshot_id, "digest": digest}),
        )
        if flat is not None:
            bundle.writestr("annotations.jsonl", "\n".join(json.dumps(r) for r in flat) + "\n")
        for name, records in (splits or {}).items():
            bundle.writestr(
                f"{name}/annotations.jsonl", "\n".join(json.dumps(r) for r in records) + "\n"
            )
    return buffer.getvalue()


@dataclass
class FakePlatform:
    archive: bytes = b""
    snapshot_digest: str = DIGEST
    job_status: str = "succeeded"
    requests: list[httpx.Request] = field(default_factory=list)
    registered: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/v1/projects/{PROJECT}/snapshots/{SNAPSHOT}":
            return httpx.Response(200, json={"id": SNAPSHOT, "digest": self.snapshot_digest})
        if path == f"/api/v1/projects/{PROJECT}/exports" and request.method == "POST":
            return httpx.Response(202, json={"id": "job-1", "type": "export", "status": "queued"})
        if path == "/api/v1/jobs/job-1":
            return httpx.Response(
                200,
                json={"id": "job-1", "type": "export", "status": self.job_status, "error": "x"},
            )
        if path == "/api/v1/jobs/job-1/download":
            # Relative, like the local connector's signed URL.
            return httpx.Response(200, json={"url": "/blob/export.zip?sig=s", "expires_in": 900})
        if path == "/blob/export.zip":
            return httpx.Response(200, content=self.archive)
        if path == f"/api/v1/models/{MODEL}/versions" and request.method == "POST":
            body = json.loads(request.content)
            if body["snapshot_digest"] != self.snapshot_digest:
                return httpx.Response(409, json={"title": "Conflict", "detail": "digest"})
            self.registered.append(body)
            count = len(self.registered)
            return httpx.Response(201, json={"id": f"v-{count}", "version": count + 1, **body})
        return httpx.Response(404, json={"title": "Not Found", "detail": path})


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def client(platform: FakePlatform) -> Client:
    return Client(
        "https://annotate.example.com", "key-123", transport=httpx.MockTransport(platform.handler)
    )
