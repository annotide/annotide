"""Scan a project's source storage and upsert `item` rows (SRC-2, SRC-4, SRC-5).

This is how data gets into a project: list the source connector, decide what is
new or changed, and record one row per object. The bytes stay in the customer's
storage throughout — a scan reads object listings plus a few hundred header
bytes per image to learn its dimensions, never the whole file (SRC-5).

Re-running a scan is safe and expected. New sources get scanned repeatedly as
data arrives, so the operation is an upsert keyed on
``(project_id, connector_id, path)``, and an object whose ETag is unchanged is
left completely alone — including its annotations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ObjectInfo, StorageConnector
from app.connectors.errors import ConnectorError
from app.models import Connector, Item, ItemStatus, MediaType, Project
from app.schemas.item import MAX_VIEWS
from app.schemas.project import WorkflowConfig
from app.services.imaging import HEADER_BYTES, image_size
from app.services.item_flow import load_workflow
from app.services.tasks import open_annotate_tasks
from app.services.text_sources import PdfTextStatus, pdf_text_path

#: Extension -> media type. Matches the formats named in SRC-8; anything not
#: listed is skipped rather than guessed at, so a stray README or .DS_Store in
#: a container never becomes an unopenable annotation task.
EXTENSION_MEDIA_TYPES: dict[str, MediaType] = {
    # Before `.json`: suffixes are matched in this order (§5 LLM-data).
    ".llm.json": MediaType.LLM,
    # Before `.csv`, for the same reason.
    ".timeseries.csv": MediaType.TIMESERIES,
    ".jpg": MediaType.IMAGE,
    ".jpeg": MediaType.IMAGE,
    ".png": MediaType.IMAGE,
    ".gif": MediaType.IMAGE,
    ".bmp": MediaType.IMAGE,
    ".webp": MediaType.IMAGE,
    ".tif": MediaType.IMAGE,
    ".tiff": MediaType.IMAGE,
    ".mp4": MediaType.VIDEO,
    ".mov": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO,
    ".wav": MediaType.AUDIO,
    ".mp3": MediaType.AUDIO,
    ".flac": MediaType.AUDIO,
    ".ogg": MediaType.AUDIO,
    ".m4a": MediaType.AUDIO,
    ".txt": MediaType.TEXT,
    ".json": MediaType.TEXT,
    ".jsonl": MediaType.TEXT,
    ".csv": MediaType.TEXT,
    ".html": MediaType.TEXT,
    ".pdf": MediaType.PDF,
}

#: Formats whose dimensions `app.services.imaging` can read from a header.
_MEASURABLE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


@dataclass(slots=True)
class ScanResult:
    """What a scan did, in the shape the `job.result` column stores."""

    scanned: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    tasks_opened: int = 0
    #: Files attached to an item as companion views instead (§5 multimodal).
    companions: int = 0
    #: New PDFs taken in as text items, whose text `extract_text` still has to read.
    pdf_texts: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "items_scanned": self.scanned,
            "items_created": self.created,
            "items_updated": self.updated,
            "items_skipped": self.skipped,
            "tasks_opened": self.tasks_opened,
            "companions": self.companions,
            "pdf_texts_pending": self.pdf_texts,
            "errors": self.errors[:20],
        }


def media_type_for(path: str) -> MediaType | None:
    """The media type for a path, or None when the extension is not supported."""
    lowered = path.lower()
    for suffix, media_type in EXTENSION_MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media_type
    return None


def pdf_text_mode(project: Project | None) -> bool:
    """Whether the project takes `.pdf` files in as text (`settings.pdf_mode: text`)."""
    return project is not None and (project.settings or {}).get("pdf_mode") == "text"


def _is_measurable(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(suffix) for suffix in _MEASURABLE_SUFFIXES)


async def _read_dimensions(
    storage: StorageConnector, path: str, result: ScanResult
) -> tuple[int | None, int | None]:
    """Read width/height from the object's header, tolerating failure.

    A range read of a couple of kilobytes, not a download (SRC-5). Any failure
    here degrades the item to "dimensions unknown" rather than failing the scan:
    one unreadable file must not stop a million-object container from importing.
    """
    if not _is_measurable(path):
        return None, None
    try:
        header = await storage.read(path, start=0, end=HEADER_BYTES)
    except (ConnectorError, OSError) as exc:
        result.errors.append(f"{path}: could not read header ({type(exc).__name__})")
        return None, None

    size = image_size(header)
    if size is None:
        result.errors.append(f"{path}: unrecognised image header")
        return None, None
    return size


async def scan_source(
    session: AsyncSession,
    *,
    project_id: UUID,
    connector: Connector,
    storage: StorageConnector,
    prefix: str = "",
    glob: str | None = None,
    limit: int | None = None,
) -> ScanResult:
    """List the source and upsert one `item` per supported object.

    The caller owns the transaction and commits. `limit` caps how many objects
    are considered, which keeps an accidental scan of an enormous container
    from running away during a demo.
    """
    result = ScanResult()
    config = await load_workflow(session, project_id)

    existing_rows = await session.scalars(
        select(Item).where(Item.project_id == project_id, Item.connector_id == connector.id)
    )
    existing: dict[str, Item] = {row.path: row for row in existing_rows}
    project = await session.get(Project, project_id)
    raw_extensions = (project.settings or {}).get("companion_extensions") if project else None
    companion_extensions: frozenset[str] = (
        frozenset(str(ext) for ext in raw_extensions)
        if isinstance(raw_extensions, list)
        else frozenset()
    )

    as_text = pdf_text_mode(project)

    if not companion_extensions:
        async for obj in storage.list(prefix, glob):
            if limit is not None and result.scanned >= limit:
                break
            await _upsert_object(
                session,
                project_id=project_id,
                connector=connector,
                storage=storage,
                obj=obj,
                current=existing.get(obj.path),
                config=config,
                result=result,
                pdf_as_text=as_text,
            )
        return result

    # Companions need the whole listing: `1.txt` may come before `1.jpg`.
    objects: list[ObjectInfo] = []
    async for obj in storage.list(prefix, glob):
        if limit is not None and len(objects) >= limit:
            break
        objects.append(obj)
    views_of = companion_views(
        [obj.path for obj in objects], companion_extensions, primary=_is_primary
    )
    companion_paths = {path for views in views_of.values() for path in views}
    for obj in objects:
        if obj.path in companion_paths:
            result.scanned += 1
            result.companions += 1
            continue
        item = await _upsert_object(
            session,
            project_id=project_id,
            connector=connector,
            storage=storage,
            obj=obj,
            current=existing.get(obj.path),
            config=config,
            result=result,
            pdf_as_text=as_text,
        )
        if item is not None and obj.path in views_of:
            item.meta = merge_views(item.meta, views_of[obj.path])
    return result


def _is_primary(path: str) -> bool:
    return media_type_for(path) is not None


def _split_extension(path: str) -> tuple[str, str]:
    name_start = path.rfind("/") + 1
    dot = path.rfind(".")
    if dot <= name_start:
        return path, ""
    return path[:dot], path[dot:].lower()


def companion_views(
    paths: list[str], extensions: frozenset[str], *, primary: Callable[[str], bool]
) -> dict[str, list[str]]:
    """Primary path -> its companion paths (§5 multimodal).

    A file is a companion when its extension is in `extensions` and another
    file with the same name but a different extension, itself not a
    companion and a supported media file (`primary`), sits beside it. Every
    such primary gets the companions, in path order.
    """
    groups: dict[str, list[str]] = {}
    for path in paths:
        stem, _ = _split_extension(path)
        groups.setdefault(stem, []).append(path)
    views: dict[str, list[str]] = {}
    for members in groups.values():
        companions = sorted(p for p in members if _split_extension(p)[1] in extensions)
        primaries = [p for p in members if _split_extension(p)[1] not in extensions and primary(p)]
        if not companions or not primaries:
            continue
        for path in primaries:
            views[path] = companions
    return views


def merge_views(meta: dict[str, Any] | None, paths: list[str]) -> dict[str, Any]:
    """`meta` with `paths` added to `meta.views`, keeping existing labels and order."""
    current = dict(meta or {})
    views = [v for v in current.get("views") or [] if isinstance(v, dict) and "path" in v]
    known = {v["path"] for v in views}
    views.extend({"path": path} for path in paths if path not in known)
    current["views"] = views[:MAX_VIEWS]
    return current


async def _upsert_object(
    session: AsyncSession,
    *,
    project_id: UUID,
    connector: Connector,
    storage: StorageConnector,
    obj: ObjectInfo,
    current: Item | None,
    config: WorkflowConfig,
    result: ScanResult,
    pdf_as_text: bool = False,
) -> Item | None:
    """Record one listed object: create its item, refresh a changed one, or leave it.

    Returns the item (new, refreshed or untouched), or None for a skipped file.
    """
    result.scanned += 1

    media_type = media_type_for(obj.path)
    if media_type is None:
        result.skipped += 1
        return None
    # PDF text mode: a new PDF is a text item until `extract_text` has read it.
    as_text = pdf_as_text and media_type is MediaType.PDF
    if as_text:
        media_type = MediaType.TEXT

    if current is not None:
        # SRC-4: unchanged content is left alone entirely. Re-measuring and
        # re-writing would churn `updated_at` on every scan and tell a
        # reviewer an item changed when it did not.
        if current.etag and obj.etag and current.etag == obj.etag:
            return current

        current.etag = obj.etag
        current.size_bytes = obj.size_bytes
        if media_type is MediaType.IMAGE:
            width, height = await _read_dimensions(storage, obj.path, result)
            if width is not None:
                current.width, current.height = width, height
        result.updated += 1
        return current

    width = height = None
    if media_type is MediaType.IMAGE:
        width, height = await _read_dimensions(storage, obj.path, result)

    item = Item(
        project_id=project_id,
        connector_id=connector.id,
        path=obj.path,
        media_type=media_type,
        etag=obj.etag,
        size_bytes=obj.size_bytes,
        width=width,
        height=height,
        meta={"content_type": obj.content_type} if obj.content_type else {},
        status=ItemStatus.NEW,
    )
    session.add(item)
    result.created += 1

    if as_text:
        # No task until the text is ready (CONTRACTS.md *PDF text mode*).
        await session.flush()
        item.meta = {
            **(item.meta or {}),
            "pdf_text": {"status": PdfTextStatus.PENDING, "path": pdf_text_path(item.id)},
            "views": [{"path": obj.path, "label": "PDF"}],
        }
        result.pdf_texts += 1
        return item

    # One annotate task per new item (WF-2). The UUID primary key is
    # assigned at flush, not at construction, so flush first or the task
    # would reference `NULL`. Re-scans never reach this branch — an
    # existing item is handled above — so this never double-opens a task.
    await session.flush()
    opened = await open_annotate_tasks(
        session, item_id=item.id, project_id=project_id, config=config
    )
    result.tasks_opened += len(opened)
    return item


async def _lookup(storage: StorageConnector, path: str) -> ObjectInfo | None:
    """The one object at `path`, or None.

    A prefix listing, since that is all every connector offers: `prefix`
    matches the path itself and anything under it, so only the exact match
    counts.
    """
    async for obj in storage.list(path):
        if obj.path == path:
            return obj
    return None


async def scan_paths(
    session: AsyncSession,
    *,
    project_id: UUID,
    connector: Connector,
    storage: StorageConnector,
    paths: list[str],
) -> ScanResult:
    """Upsert the named objects only, as a scan would (SRC-3).

    A store event names what changed, so there is nothing to list: each path
    is looked up on its own. One the store no longer has (deleted since the
    event, or never under this connector) is skipped and noted in `errors`,
    not failed: events arrive late and more than once. The caller commits.
    """
    result = ScanResult()
    config = await load_workflow(session, project_id)
    as_text = pdf_text_mode(await session.get(Project, project_id))
    wanted = list(dict.fromkeys(paths))
    existing_rows = await session.scalars(
        select(Item).where(
            Item.project_id == project_id,
            Item.connector_id == connector.id,
            Item.path.in_(wanted),
        )
    )
    existing: dict[str, Item] = {row.path: row for row in existing_rows}

    for path in wanted:
        try:
            obj = await _lookup(storage, path)
        except ConnectorError as exc:
            result.errors.append(f"{path}: could not look up ({type(exc).__name__})")
            result.skipped += 1
            continue
        if obj is None:
            result.errors.append(f"{path}: not in the source")
            result.skipped += 1
            continue
        await _upsert_object(
            session,
            project_id=project_id,
            connector=connector,
            storage=storage,
            obj=obj,
            current=existing.get(path),
            config=config,
            result=result,
            pdf_as_text=as_text,
        )
    return result
