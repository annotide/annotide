"""A scripted fake of the platform on `httpx.MockTransport` — no network."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from annotide import Client

BASE = "https://annotate.example.com"
KEY = "ak_test"

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeApi:
    """Routes `(method, path)` to queued replies and records every request.

    A route answers its replies in order and repeats the last one, so a
    single reply serves any number of calls.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes[(method, path)] = list(replies)

    def json(self, method: str, path: str, *bodies: Any, status: int = 200) -> None:
        self.on(method, path, *(httpx.Response(status, json=body) for body in bodies))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        replies = self.routes.get((request.method, request.url.path))
        if not replies:
            return httpx.Response(
                404, json={"type": "about:blank", "title": "Not Found", "status": 404}
            )
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return reply(request) if callable(reply) else reply

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def client(self, **kwargs: Any) -> Client:
        return Client(
            BASE,
            KEY,
            transport=httpx.MockTransport(self.handle),
            sleep=self.sleeps.append,
            **kwargs,
        )


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


def job(status: str = "succeeded", **overrides: Any) -> dict[str, Any]:
    return {
        "id": "j1",
        "project_id": "p1",
        "type": "export",
        "status": status,
        "progress": 100 if status == "succeeded" else 0,
        "payload": {},
        "result": None,
        "error": None,
        "attempts": 1,
        "started_at": None,
        "finished_at": None,
        "created_at": "2026-09-25T00:00:00Z",
        "updated_at": "2026-09-25T00:00:00Z",
        **overrides,
    }


def problem(status: int, title: str, detail: str | None = None) -> dict[str, Any]:
    return {"type": "about:blank", "title": title, "status": status, "detail": detail}
