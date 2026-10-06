"""Verify and parse `retrain.requested` deliveries (CONTRACTS.md, webhooks).

Mirrors `backend/app/services/webhooks.py::verify`: the header is
`t=<unix seconds>,v1=<hex HMAC-SHA256 of "{t}.{body}">`, stale timestamps
are rejected and the comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

SIGNATURE_HEADER = "X-Annotation-Signature"
EVENT_HEADER = "X-Annotation-Event"
DELIVERY_HEADER = "X-Annotation-Delivery"
RETRAIN_EVENT = "retrain.requested"
TOLERANCE_SECONDS = 300


class WebhookError(ValueError):
    """The delivery is not one this pipeline acts on."""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify(
    secret: str,
    header: str,
    body: bytes,
    *,
    now: float | None = None,
    tolerance_seconds: int = TOLERANCE_SECONDS,
) -> bool:
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


@dataclass(frozen=True, slots=True)
class RetrainRequest:
    """What one `retrain.requested` event asks for."""

    project_id: str
    snapshot_id: str
    snapshot_digest: str
    model_id: str | None
    note: str | None
    delivery_id: str | None


def parse_retrain(body: bytes) -> RetrainRequest:
    """Parse a verified delivery body; `WebhookError` unless it names a snapshot."""
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookError("body is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("event") != RETRAIN_EVENT:
        raise WebhookError(f"not a {RETRAIN_EVENT} event")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise WebhookError("event has no data")
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("id") or not snapshot.get("digest"):
        # The platform allows a retrain request without a snapshot; there is
        # nothing to train on until someone freezes one.
        raise WebhookError("retrain.requested names no snapshot")
    project_id = data.get("project_id") or payload.get("project_id")
    if not project_id:
        raise WebhookError("event has no project_id")
    model_id = data.get("model_id")
    note = data.get("note")
    delivery = payload.get("delivery_id")
    return RetrainRequest(
        project_id=str(project_id),
        snapshot_id=str(snapshot["id"]),
        snapshot_digest=str(snapshot["digest"]),
        model_id=str(model_id) if model_id else None,
        note=str(note) if note else None,
        delivery_id=str(delivery) if delivery else None,
    )
