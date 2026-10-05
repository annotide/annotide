"""Outbound webhooks (API-4): subscriptions, signed delivery, retry.

Two halves, joined by the `webhook_delivery` table so an event is never
lost between the request that caused it and the HTTP call that reports it:

- `emit_event` runs inside the caller's transaction and writes one delivery
  row per matching active hook. It never talks to the network, so a slow or
  dead subscriber cannot slow down a submit.
- `deliver_due` runs from the worker cron, claims due rows with
  `FOR UPDATE SKIP LOCKED`, POSTs them with an HMAC-SHA256 signature and
  schedules the next attempt with exponential backoff until
  `APP_WEBHOOK_MAX_ATTEMPTS` is spent.

Signature: `X-Annotation-Signature: t=<unix seconds>,v1=<hex hmac>` over
`"{t}.{body}"` with the hook's secret, the same scheme Stripe and GitHub
subscribers already know how to verify. `X-Annotation-Event` names the event
and `X-Annotation-Delivery` the delivery id, so a receiver can de-duplicate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.netguard import public_destination, public_only_transport
from app.core.security import UnsealError, seal, unseal
from app.models import Project, Webhook, WebhookDelivery, WebhookDeliveryStatus, WebhookFormat
from app.schemas.webhook import EVENTS, WILDCARD
from app.services import chat_messages, rate_limit

log = structlog.get_logger(__name__)

__all__ = [
    "EVENTS",
    "WILDCARD",
    "deliver",
    "deliver_due",
    "emit_event",
    "generate_secret",
    "public_destination",
    "public_only_transport",
    "sign",
    "verify",
]

SIGNATURE_HEADER = "X-Annotation-Signature"
EVENT_HEADER = "X-Annotation-Event"
DELIVERY_HEADER = "X-Annotation-Delivery"

#: First retry after 30 s, then doubling: 30 s, 1 min, 2, 4, 8, 16, 32 min, ...
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 6 * 60 * 60


#: `webhook.secret` holds the signing secret sealed under `APP_SECRET_KEY`
#: (AES-GCM, this purpose), never in clear: a database dump alone cannot forge
#: deliveries. Rotating `APP_SECRET_KEY` means rotating every hook's secret.
SEAL_PURPOSE = "webhook-secret"


def generate_secret() -> str:
    """A fresh signing secret: 32 random bytes, hex — shown to the creator once."""
    return secrets.token_hex(32)


def seal_secret(secret: str) -> str:
    """The value stored in `webhook.secret` for a signing secret."""
    return seal(secret, purpose=SEAL_PURPOSE)


def signing_secret(hook: Webhook) -> str:
    """The hook's signing secret in clear. Raises `UnsealError` after a key change."""
    return unseal(hook.secret, purpose=SEAL_PURPOSE)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """`t=<timestamp>,v1=<hmac-sha256 hex>` over `"{timestamp}.{body}"`."""
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, header: str, body: bytes, *, tolerance_seconds: int = 300) -> bool:
    """Reference verifier for subscribers (and our tests): parse the header,
    reject stale timestamps, compare in constant time."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(int(datetime.now(UTC).timestamp()) - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected.encode(), parts.get("v1", "").encode())


def backoff_seconds(attempt: int) -> int:
    """Delay before attempt number `attempt + 1` (attempt counts from 1)."""
    return int(min(_BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1), _BACKOFF_MAX_SECONDS))


def subscribes(hook: Webhook, event: str) -> bool:
    return WILDCARD in hook.events or event in hook.events


async def emit_event(
    session: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID | None,
    event: str,
    payload: dict[str, Any],
) -> int:
    """Queue `event` for every active hook of the organisation that listens to
    it and covers `project_id` (or all projects). Adds rows to the caller's
    session, does not commit. Returns how many deliveries were queued."""
    conditions = [Webhook.organization_id == organization_id, Webhook.is_active.is_(True)]
    if project_id is None:
        conditions.append(Webhook.project_id.is_(None))
    else:
        conditions.append(or_(Webhook.project_id.is_(None), Webhook.project_id == project_id))
    hooks = [
        h for h in await session.scalars(select(Webhook).where(*conditions)) if subscribes(h, event)
    ]
    body = {
        "event": event,
        "occurred_at": datetime.now(UTC).isoformat(),
        "organization_id": str(organization_id),
        "project_id": str(project_id) if project_id else None,
        "data": payload,
    }
    for hook in hooks:
        session.add(WebhookDelivery(webhook_id=hook.id, event=event, payload=body))
    return len(hooks)


class _DestinationRefusedError(Exception):
    """The guard refused the hook's URL; the message says why."""


#: Resolves a hook URL and says why it may not be called, or None (SEC-4).
DestinationGuard = Callable[[str], Awaitable[str | None]]


def render_body(
    delivery: WebhookDelivery,
    hook_format: WebhookFormat | None = WebhookFormat.JSON,
    *,
    project_name: str | None = None,
    frontend_url: str | None = None,
) -> bytes:
    """The exact bytes sent (and signed).

    `json`: compact, key-sorted JSON with the delivery id. `slack` / `teams`:
    a chat message for the same event (API-7, `chat_messages`).
    """
    document: dict[str, Any] = {**delivery.payload, "delivery_id": str(delivery.id)}
    # Anything but a chat format (including an unset one) is the event itself.
    if hook_format in {WebhookFormat.SLACK, WebhookFormat.TEAMS}:
        message = chat_messages.summarise(
            document, project_name=project_name, frontend_url=frontend_url
        )
        if hook_format is WebhookFormat.SLACK:
            document = chat_messages.slack_body(message)
        else:
            document = chat_messages.teams_body(message)
    return json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


async def deliver(
    delivery: WebhookDelivery,
    hook: Webhook,
    client: httpx.AsyncClient,
    *,
    max_attempts: int,
    now: datetime | None = None,
    project_name: str | None = None,
    frontend_url: str | None = None,
    guard: DestinationGuard | None = None,
) -> bool:
    """One attempt at `delivery`. Mutates the rows; the caller commits.

    With a `guard` (the worker passes `public_destination` unless
    `APP_WEBHOOK_ALLOW_PRIVATE_URLS`), a refused destination counts as a
    failed attempt and nothing is sent.

    Any 2xx is success. Anything else — a 4xx, a 5xx, a timeout, a refused
    connection — schedules the next attempt, or marks the delivery failed
    once `max_attempts` is spent. Returns True on success.
    """
    now = now or datetime.now(UTC)
    body = render_body(delivery, hook.format, project_name=project_name, frontend_url=frontend_url)
    timestamp = int(now.timestamp())
    delivery.attempts += 1
    hook.last_delivery_at = now
    refused = await guard(hook.url) if guard is not None else None
    try:
        if refused is not None:
            raise _DestinationRefusedError(refused)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "annotide-webhooks/1",
            SIGNATURE_HEADER: sign(signing_secret(hook), timestamp, body),
            EVENT_HEADER: delivery.event,
            DELIVERY_HEADER: str(delivery.id),
        }
        response = await client.post(hook.url, content=body, headers=headers)
    except UnsealError:
        # `APP_SECRET_KEY` changed since the secret was sealed: nothing can be
        # signed until an admin rotates it. Retried like any failed attempt.
        delivery.response_status = None
        delivery.error = "signing secret cannot be opened; rotate the webhook secret"
        hook.last_response_status = None
    except _DestinationRefusedError as exc:
        delivery.response_status = None
        delivery.error = f"destination not allowed: {exc}"
        hook.last_response_status = None
    except httpx.HTTPError as exc:
        delivery.response_status = None
        delivery.error = f"{type(exc).__name__}: {exc}"
        hook.last_response_status = None
    else:
        delivery.response_status = response.status_code
        hook.last_response_status = response.status_code
        if 200 <= response.status_code < 300:
            delivery.status = WebhookDeliveryStatus.SUCCEEDED
            delivery.delivered_at = now
            delivery.error = None
            return True
        delivery.error = f"HTTP {response.status_code}"

    if delivery.attempts >= max_attempts:
        delivery.status = WebhookDeliveryStatus.FAILED
    else:
        delivery.next_attempt_at = now + timedelta(seconds=backoff_seconds(delivery.attempts))
    return False


async def deliver_due(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    batch_size: int,
    max_attempts: int,
    now: datetime | None = None,
    frontend_url: str | None = None,
    guard: DestinationGuard | None = None,
    per_hook_per_minute: int | None = None,
    per_hook_per_batch: int | None = None,
) -> dict[str, int]:
    """Send every pending delivery whose time has come. Commits.

    `per_hook_per_batch` caps one hook's share of the batch, so a hook with a
    backlog cannot crowd out the others; `per_hook_per_minute` is the hook's
    budget (a fixed one-minute window in Redis, failing open like the API's
    limiter). A delivery over budget is not attempted: it moves to the start
    of the next window, its attempt count untouched, and counts as deferred.
    """
    now = now or datetime.now(UTC)
    due_filter = (
        WebhookDelivery.status == WebhookDeliveryStatus.PENDING,
        WebhookDelivery.next_attempt_at <= now,
    )
    order = (WebhookDelivery.next_attempt_at, WebhookDelivery.created_at)
    stmt = select(WebhookDelivery)
    if per_hook_per_batch is not None:
        # Row number per hook in delivery order; PostgreSQL refuses FOR UPDATE
        # next to a window function, so the ranking is a subquery of ids.
        ranked = (
            select(
                WebhookDelivery.id,
                func.row_number()
                .over(partition_by=WebhookDelivery.webhook_id, order_by=order)
                .label("rank"),
            )
            .where(*due_filter)
            .subquery()
        )
        stmt = stmt.where(
            WebhookDelivery.id.in_(select(ranked.c.id).where(ranked.c.rank <= per_hook_per_batch))
        )
    due: Sequence[WebhookDelivery] = list(
        await session.scalars(
            stmt.where(*due_filter)
            .order_by(*order)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if not due:
        return {"sent": 0, "retried": 0, "failed": 0, "deferred": 0}

    hooks = {
        h.id: h
        for h in await session.scalars(
            select(Webhook).where(Webhook.id.in_({d.webhook_id for d in due}))
        )
    }
    # Chat formats name the project; one query for the whole batch.
    project_ids = {
        UUID(str(d.payload["project_id"]))
        for d in due
        if d.payload.get("project_id")
        and (hook := hooks.get(d.webhook_id)) is not None
        and hook.format is not WebhookFormat.JSON
    }
    names: dict[str, str] = {}
    if project_ids:
        rows = await session.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        names = {str(project_id): name for project_id, name in rows}
    tally = {"sent": 0, "retried": 0, "failed": 0, "deferred": 0}
    for delivery in due:
        hook = hooks.get(delivery.webhook_id)
        if hook is None or not hook.is_active:
            # Deactivated between emit and send: drop it rather than knock on
            # a door the owner closed.
            delivery.status = WebhookDeliveryStatus.FAILED
            delivery.error = "webhook inactive"
            tally["failed"] += 1
            continue
        if per_hook_per_minute is not None:
            budget = await rate_limit.get_rate_limiter().hit(
                "webhook", str(hook.id), per_hook_per_minute
            )
            if not budget.allowed:
                delivery.next_attempt_at = now + timedelta(seconds=budget.reset_after)
                tally["deferred"] += 1
                continue
        ok = await deliver(
            delivery,
            hook,
            client,
            max_attempts=max_attempts,
            now=now,
            project_name=names.get(str(delivery.payload.get("project_id"))),
            frontend_url=frontend_url,
            guard=guard,
        )
        if ok:
            tally["sent"] += 1
        elif delivery.status is WebhookDeliveryStatus.FAILED:
            tally["failed"] += 1
            log.warning(
                "webhook.delivery_failed",
                delivery_id=str(delivery.id),
                webhook_id=str(hook.id),
                attempts=delivery.attempts,
                error=delivery.error,
            )
        else:
            tally["retried"] += 1
    await session.commit()
    return tally
