"""The outbox publisher (DATA-2): annotation JSON from Postgres to blob storage.

`app.services.annotations.create_version` writes an annotation row and an
`outbox_event` row in one transaction. This cron job, once a minute, claims
unpublished events with `FOR UPDATE SKIP LOCKED` (so several worker replicas
never publish the same event twice), writes each annotation document to the
project's result connector at `annotations/{project}/{item}/v{n}.json`, sets
`annotation.blob_path`, and stamps `published_at`.

A tick drains the backlog batch by batch, each batch its own transaction,
until a batch comes back short, has a failure, or `TICK_BUDGET_SECONDS` have
passed. One batch per tick capped publishing at 50 events a minute, which
fifty annotators saving drafts outrun (found by `loadtest/`, NFR-1…4). A
failure ends the tick so that storage trouble waits for the next one rather
than burning an event's attempts within seconds.

The event payload already carries the finished document (see
`build_blob_document`), so publishing needs no re-derivation — only the
connector to write it with.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import StorageConnector
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Annotation, Connector, Item, OutboxEvent, Project
from app.services.annotations import ANNOTIDE_WRITTEN
from app.services.storage import open_storage

log = get_logger(__name__)

#: Stop claiming new batches after this long; the next tick, a minute after
#: this one started, carries on. Below the cron interval so ticks never pile up.
TICK_BUDGET_SECONDS = 45.0


class _StorageCache:
    """One open connector per result connector id for the duration of a batch."""

    def __init__(self) -> None:
        self._open: dict[UUID, StorageConnector] = {}

    async def get(self, session: AsyncSession, project: Project) -> StorageConnector:
        if project.result_connector_id is None:
            raise LookupError(f"project {project.id} has no result connector")
        storage = self._open.get(project.result_connector_id)
        if storage is None:
            connector = await session.get(Connector, project.result_connector_id)
            if connector is None:
                raise LookupError(f"result connector {project.result_connector_id} does not exist")
            storage = await open_storage(connector)
            self._open[project.result_connector_id] = storage
        return storage

    async def aclose(self) -> None:
        for storage in self._open.values():
            await storage.aclose()
        self._open.clear()


async def _publish_annotation(
    session: AsyncSession, event: OutboxEvent, storages: _StorageCache
) -> str | None:
    """Write one annotation document. Returns the blob path, or None if nothing is left to write."""
    annotation = await session.get(Annotation, event.aggregate_id)
    if annotation is None:
        # Deleted since; the event has nothing left to describe.
        return None
    item = await session.get(Item, annotation.item_id)
    if item is None:
        return None
    project = await session.get(Project, item.project_id)
    if project is None:
        raise LookupError(f"project {item.project_id} does not exist")

    blob_path = str(event.payload.get("blob_path") or "")
    document: Any = event.payload.get("document")
    if not blob_path or document is None:
        raise ValueError(f"outbox event {event.id} has no blob_path/document")

    storage = await storages.get(session, project)
    await storage.write(
        blob_path,
        json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
        "application/json",
    )
    annotation.blob_path = blob_path
    return blob_path


async def _publish_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    storages: _StorageCache,
    batch_size: int,
    max_attempts: int,
) -> tuple[int, int, int]:
    """Claim and publish one batch in one transaction → (claimed, published, failed)."""
    published = 0
    failed = 0
    async with sessionmaker() as session:
        events = list(
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.attempts < max_attempts,
                )
                .order_by(OutboxEvent.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            event.attempts = event.attempts + 1
            try:
                if event.type == ANNOTIDE_WRITTEN:
                    blob_path = await _publish_annotation(session, event, storages)
                    log.info("outbox.published", event_id=str(event.id), blob_path=blob_path)
                else:
                    # Unknown event types are stamped, not retried forever:
                    # nothing here knows how to publish them.
                    log.warning("outbox.unknown_type", event_id=str(event.id), type=event.type)
                event.published_at = datetime.now(UTC)
                published += 1
            except Exception as exc:  # keep going with the rest of the batch
                failed += 1
                log.warning(
                    "outbox.publish_failed",
                    event_id=str(event.id),
                    attempts=event.attempts,
                    error=str(exc),
                )
        await session.commit()
    return len(events), published, failed


async def publish_outbox_events(ctx: dict[str, Any]) -> dict[str, Any]:
    """One publisher tick. Registered as an arq cron job in `app.worker.main`."""
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    settings = ctx.get("settings") or get_settings()
    batch_size = int(settings.outbox_poll_batch_size)
    max_attempts = int(settings.outbox_max_attempts)
    deadline = time.monotonic() + TICK_BUDGET_SECONDS

    published = 0
    failed = 0
    batches = 0
    storages = _StorageCache()
    try:
        while True:
            claimed, ok, bad = await _publish_batch(
                sessionmaker, storages, batch_size, max_attempts
            )
            published += ok
            failed += bad
            batches += 1 if claimed else 0
            if claimed < batch_size or bad or time.monotonic() >= deadline:
                break
    finally:
        await storages.aclose()

    if batches:
        log.info("outbox.batch_done", published=published, failed=failed, batches=batches)
    return {"published": published, "failed": failed}
