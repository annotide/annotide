from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from annotide_training.cli import make_handler
from annotide_training.webhook import (
    RetrainRequest,
    WebhookError,
    parse_retrain,
    sign,
    verify,
)
from tests.conftest import DIGEST, PROJECT, SNAPSHOT

SECRET = "whsec_test"


def _event(snapshot: dict[str, Any] | None = None, **data: Any) -> bytes:
    body = {
        "event": "retrain.requested",
        "occurred_at": "2026-09-24T12:00:00Z",
        "organization_id": "org",
        "project_id": PROJECT,
        "delivery_id": "d-1",
        "data": {
            "project_id": PROJECT,
            "requested_by": "u",
            "model_id": "m-1",
            "note": None,
            "snapshot": {"id": SNAPSHOT, "digest": DIGEST} if snapshot is None else snapshot,
            **data,
        },
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def test_verify_accepts_fresh_and_rejects_stale_or_tampered() -> None:
    body = _event()
    now = 1_800_000_000
    header = sign(SECRET, now, body)
    assert verify(SECRET, header, body, now=now + 10)
    assert not verify(SECRET, header, body, now=now + 301)
    assert not verify(SECRET, header, body + b" ", now=now)
    assert not verify("other", header, body, now=now)
    assert not verify(SECRET, "garbage", body, now=now)


def test_parse_retrain() -> None:
    request = parse_retrain(_event())
    assert request == RetrainRequest(PROJECT, SNAPSHOT, DIGEST, "m-1", None, "d-1")
    with pytest.raises(WebhookError, match="no snapshot"):
        parse_retrain(_event(snapshot={}))
    with pytest.raises(WebhookError, match="not a retrain"):
        parse_retrain(json.dumps({"event": "snapshot.created"}).encode())
    with pytest.raises(WebhookError, match="JSON"):
        parse_retrain(b"{")


def test_receiver_verifies_hands_off_and_deduplicates() -> None:
    received: list[RetrainRequest] = []
    done = threading.Event()

    def on_request(request: RetrainRequest) -> None:
        received.append(request)
        done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SECRET, on_request))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        body = _event()
        signed = {
            "X-Annotation-Signature": sign(SECRET, int(time.time()), body),
            "X-Annotation-Delivery": "d-1",
        }

        assert httpx.post(url, content=body, headers=signed).status_code == 202
        assert done.wait(5)
        assert httpx.post(url, content=body, headers=signed).status_code == 200  # duplicate
        bad = {**signed, "X-Annotation-Signature": sign("nope", int(time.time()), body)}
        assert httpx.post(url, content=body, headers=bad).status_code == 401
        other = _event(snapshot={})
        ignored = {"X-Annotation-Signature": sign(SECRET, int(time.time()), other)}
        assert httpx.post(url, content=other, headers=ignored).status_code == 200
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()
