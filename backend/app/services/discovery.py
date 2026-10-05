"""Event-driven discovery (SRC-3): store notifications become targeted scans.

A scan lists the whole source, which is the right tool for a first import
and a poor one for a container that grows by a few files an hour. Object
stores can instead announce each new object — Azure Event Grid, S3 event
notifications (through SNS or EventBridge, or posted directly by MinIO),
GCS through Pub/Sub — and this module turns one such delivery into the
paths each project should pick up.

Parsing is deliberately forgiving about what it does not need: a delivery
names objects, and anything else in it (event types this platform does not
act on, records missing a key) is dropped, not refused. Only a body that is
none of the known shapes is an error, so a misrouted subscription shows up
as 422s in the sender's delivery log rather than as silence.

Deletes are parsed and then ignored: an item carries annotations, and a
store event is no reason to drop them, just as a scan never does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import unquote_plus, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import BaseStorageConnector
from app.models import Connector, Project
from app.services.scanning import media_type_for

#: Event Grid batches are at most 1 MB; nothing legitimate is larger.
MAX_EVENT_BODY_BYTES = 1024 * 1024
#: Objects named in one delivery, after parsing.
MAX_EVENT_OBJECTS = 5_000

_TOKEN_PREFIX = "evt_"

_EVENT_GRID_VALIDATION = "Microsoft.EventGrid.SubscriptionValidationEvent"
_AZURE_SUBJECT = re.compile(
    r"^/blobServices/default/containers/(?P<container>[^/]+)/blobs/(?P<path>.+)$"
)
_SNS_TYPES = {"SubscriptionConfirmation", "Notification", "UnsubscribeConfirmation"}
_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_SNS_CONFIRM_TIMEOUT_SECONDS = 10


class EventFormatError(ValueError):
    """The body is none of the notification shapes this endpoint accepts."""


class ObjectChange(StrEnum):
    CREATED = "created"
    DELETED = "deleted"


_AZURE_TYPES = {
    "Microsoft.Storage.BlobCreated": ObjectChange.CREATED,
    "Microsoft.Storage.BlobDeleted": ObjectChange.DELETED,
}
_EVENTBRIDGE_TYPES = {
    "Object Created": ObjectChange.CREATED,
    "Object Deleted": ObjectChange.DELETED,
}
_GCS_TYPES = {
    "OBJECT_FINALIZE": ObjectChange.CREATED,
    "OBJECT_DELETE": ObjectChange.DELETED,
}


@dataclass(frozen=True, slots=True)
class ObjectEvent:
    """One object a delivery names. `container` is None when the sender omits it."""

    change: ObjectChange
    container: str | None
    path: str


@dataclass(slots=True)
class Delivery:
    """What one POST carried: object events, or a subscription handshake."""

    events: list[ObjectEvent] = field(default_factory=list)
    #: Event Grid's subscription validation code, echoed back to prove ownership.
    validation_code: str | None = None
    #: SNS's `SubscribeURL`, fetched to confirm the subscription.
    subscribe_url: str | None = None


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #


def hash_event_token(token: str) -> str:
    """SHA-256 hex: the token is 256 random bits, so a fast hash is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_event_token() -> tuple[str, str]:
    """``(token to show once, hash to store)``."""
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_event_token(token)


def token_matches(connector: Connector, token: str | None) -> bool:
    """Whether `token` opens this connector's event endpoint; False when events are off."""
    if not token or connector.event_token_hash is None:
        return False
    return hmac.compare_digest(connector.event_token_hash, hash_event_token(token))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_event_grid(entries: Iterable[Any]) -> Delivery:
    """Event Grid schema (`eventType`) and CloudEvents 1.0 (`type`) alike."""
    delivery = Delivery()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("eventType") or entry.get("type")
        if kind == _EVENT_GRID_VALIDATION:
            code = _text(_dig(entry, "data", "validationCode"))
            if code is None:
                raise EventFormatError("the validation event carries no validationCode")
            return Delivery(validation_code=code)
        change = _AZURE_TYPES.get(str(kind))
        match = _AZURE_SUBJECT.match(str(entry.get("subject") or ""))
        if change is None or match is None:
            continue
        delivery.events.append(ObjectEvent(change, match["container"], match["path"]))
    return delivery


def _parse_s3_records(body: dict[str, Any]) -> list[ObjectEvent]:
    """S3 notification records; MinIO prefixes the event name with `s3:`."""
    records = body.get("Records")
    if not isinstance(records, list):
        return []
    events: list[ObjectEvent] = []
    for record in records:
        name = str(_dig(record, "eventName") or "").removeprefix("s3:")
        if name.startswith("ObjectCreated:"):
            change = ObjectChange.CREATED
        elif name.startswith("ObjectRemoved:"):
            change = ObjectChange.DELETED
        else:
            continue
        key = _text(_dig(record, "s3", "object", "key"))
        if key is None:
            continue
        # S3 URL-encodes the key in notifications, a space as `+`.
        bucket = _text(_dig(record, "s3", "bucket", "name"))
        events.append(ObjectEvent(change, bucket, unquote_plus(key)))
    return events


def _parse_eventbridge(body: dict[str, Any]) -> list[ObjectEvent]:
    change = _EVENTBRIDGE_TYPES.get(str(body.get("detail-type")))
    key = _text(_dig(body, "detail", "object", "key"))
    if change is None or key is None:
        return []
    return [ObjectEvent(change, _text(_dig(body, "detail", "bucket", "name")), key)]


def _parse_aws(body: dict[str, Any]) -> list[ObjectEvent] | None:
    """S3 records or an EventBridge event; None when the body is neither."""
    if "Records" in body or body.get("Event") == "s3:TestEvent":
        return _parse_s3_records(body)
    if body.get("source") == "aws.s3":
        return _parse_eventbridge(body)
    return None


def _parse_sns(body: dict[str, Any]) -> Delivery:
    kind = body.get("Type")
    if kind == "SubscriptionConfirmation":
        url = _text(body.get("SubscribeURL"))
        if url is None:
            raise EventFormatError("the SNS confirmation carries no SubscribeURL")
        return Delivery(subscribe_url=url)
    if kind != "Notification":
        return Delivery()
    try:
        message = json.loads(str(body.get("Message") or ""))
    except ValueError as exc:
        raise EventFormatError("the SNS message is not JSON") from exc
    events = _parse_aws(message) if isinstance(message, dict) else None
    if events is None:
        raise EventFormatError("the SNS message is not an S3 event")
    return Delivery(events=events)


def _parse_pubsub(message: dict[str, Any]) -> list[ObjectEvent]:
    attributes = message.get("attributes")
    change = _GCS_TYPES.get(str(_dig(attributes, "eventType")))
    path = _text(_dig(attributes, "objectId"))
    if change is None or path is None:
        return []
    return [ObjectEvent(change, _text(_dig(attributes, "bucketId")), path)]


def parse_delivery(body: Any) -> Delivery:
    """Recognise the sender by the body's shape and pull out the objects it names."""
    if isinstance(body, list):
        return _parse_event_grid(body)
    if not isinstance(body, dict):
        raise EventFormatError("the body is not a JSON object or array")
    if "specversion" in body or "eventType" in body:
        return _parse_event_grid([body])
    if body.get("Type") in _SNS_TYPES:
        return _parse_sns(body)
    aws = _parse_aws(body)
    if aws is not None:
        return Delivery(events=aws)
    message = body.get("message")
    if isinstance(message, dict) and isinstance(message.get("attributes"), dict):
        return Delivery(events=_parse_pubsub(message))
    raise EventFormatError(
        "not an Event Grid, CloudEvents, S3, SNS, EventBridge or Pub/Sub notification"
    )


# --------------------------------------------------------------------------- #
# SNS handshake
# --------------------------------------------------------------------------- #


def is_sns_url(url: str) -> bool:
    """An `https://sns.<region>.amazonaws.com[.cn]/` URL and nothing else.

    The URL comes from the request body, so it is checked before the API
    fetches it: without this the endpoint would GET whatever it was handed.
    """
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.port in (None, 443)
        and bool(_SNS_HOST.fullmatch(parts.hostname or ""))
    )


async def confirm_sns_subscription(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """GET the `SubscribeURL`, which is how SNS confirms an HTTPS subscription."""
    if not is_sns_url(url):
        raise EventFormatError("SubscribeURL is not an Amazon SNS endpoint")
    async with httpx.AsyncClient(
        transport=transport, timeout=_SNS_CONFIRM_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        response = await client.get(url)
        response.raise_for_status()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def connector_container(connector: Connector) -> str | None:
    """The container or bucket the connector reads; None for `local` and `http`."""
    for key in ("container", "bucket"):
        value = connector.config.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def route_events(
    session: AsyncSession, connector: Connector, events: Iterable[ObjectEvent]
) -> dict[UUID, list[str]]:
    """The created paths each project sourcing from `connector` should pick up.

    A path goes to a project when it is under the project's `source_prefix`,
    matches its `source_glob` and has a supported extension — what a full
    scan of that project would have registered. Projects with nothing to
    pick up are left out.
    """
    container = connector_container(connector)
    created = list(
        dict.fromkeys(
            event.path
            for event in events
            if event.change is ObjectChange.CREATED
            and (container is None or event.container in (None, container))
            and media_type_for(event.path) is not None
        )
    )
    if not created:
        return {}

    projects = await session.scalars(
        select(Project)
        .where(Project.source_connector_id == connector.id)
        .order_by(Project.created_at, Project.id)
    )
    routed: dict[UUID, list[str]] = {}
    for project in projects:
        # The connectors' own prefix/glob rule, so an event and a scan agree.
        paths = [
            path
            for path in created
            if BaseStorageConnector._matches_prefix_and_glob(
                path, project.source_prefix or "", project.source_glob
            )
        ]
        if paths:
            routed[project.id] = paths
    return routed
