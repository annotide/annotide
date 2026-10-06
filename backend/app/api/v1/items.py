"""Item listing, registration, retrieval and skip (WF-7, TOOL-6, AUTH-6, SRC-2).

Thin by design: item registration is idempotent because source scans re-run
and must not duplicate rows, media URLs are always minted fresh and
short-lived through the item's connector, and skipping an item is just
another edge of the state machine in `app.services.workflow`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    UNRESTRICTED_LICENCE,
    ClientIpDep,
    CurrentUserDep,
    PageParamsDep,
    SessionDep,
    SettingsDep,
)
from app.api.errors import (
    ConflictError,
    ForbiddenError,
    ModelUnavailableError,
    ValidationFailedError,
)
from app.connectors.errors import ConnectorError
from app.core.config import Settings
from app.models import Connector, Item, Model, ModelTask, Project
from app.models import MediaType as ModelMediaType
from app.schemas import (
    BulkRequest,
    BulkResult,
    InteractiveRequest,
    InteractiveResult,
    ItemCreate,
    ItemRead,
    ItemView,
    OcrRequest,
    OcrResult,
    OcrWord,
    Page,
)
from app.schemas import ItemStatus as SchemaItemStatus
from app.schemas import MediaType as SchemaMediaType
from app.schemas.split import SplitRequest, SplitResponse
from app.schemas.task import TaskRead
from app.schemas.tiles import TileSignRequest, TileSignResponse
from app.services import audit
from app.services.bulk import bulk_items
from app.services.item_flow import load_workflow
from app.services.item_flow import skip_item as apply_skip
from app.services.models import ModelClient, ModelRejected, ModelUnavailable, PredictItem
from app.services.repository import (
    ensure_item_member,
    ensure_project_member,
    get_or_404,
    member_prefixes,
    paginate,
    path_in_scope,
    path_scope_clause,
)
from app.services.scanning import media_type_for
from app.services.secrets import SecretResolutionError
from app.services.splitting import split_item as apply_split
from app.services.storage import storage_for
from app.services.text_sources import TextSource, pdf_text_meta, text_source
from app.services.tiling import TILE_SUFFIX, tile_blob_path, validate_tile_coordinate

router = APIRouter(tags=["items"])


class SkipRequest(BaseModel):
    """Payload for `POST /items/{item_id}/skip`; a reason is mandatory (TOOL-6)."""

    reason: str = Field(min_length=1, description="Why this item is being skipped")


def _has_tag(tag: str) -> Any:
    """SQL: `item.meta.tags` (a JSON array of strings) contains `tag` exactly.

    Matches the quoted JSON string inside the array's text form, which reads
    the same on PostgreSQL and SQLite; LIKE wildcards in the tag are escaped.
    """
    needle = json.dumps(tag, ensure_ascii=False)
    for char in ("\\", "%", "_"):
        needle = needle.replace(char, "\\" + char)
    return cast(Item.meta["tags"], Text).like(f"%{needle}%", escape="\\")


@router.get(
    "/projects/{project_id}/items",
    response_model=Page[ItemRead],
    summary="List items in a project",
)
async def list_items(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    page: PageParamsDep,
    settings: SettingsDep,
    item_status: Annotated[SchemaItemStatus | None, Query(alias="status")] = None,
    media_type: Annotated[SchemaMediaType | None, Query()] = None,
    assignee_id: Annotated[UUID | None, Query(description="Filter by assigned task")] = None,
    q: Annotated[str | None, Query(description="Substring match on path")] = None,
    tag: Annotated[
        str | None,
        Query(min_length=1, max_length=64, description="Items carrying this tag (meta.tags)"),
    ] = None,
) -> Page[ItemRead]:
    """List a project's items, cursor-paginated and filterable (WF-7).

    `assignee_id` filters by the assignee of any task on the item (items
    themselves carry no assignee — that lives on `task`). `q` is a plain
    substring match against `path`; `tag` keeps items whose `meta.tags`
    holds that exact tag (set by bulk `tag`).

    Every row carries a freshly signed `media_url` so the data browser can
    show the items rather than a grid of filenames (UX-6). It is the full
    object, not a thumbnail: real thumbnails are a worker job that phase 1
    does not build yet (IMG-8). `null` where the connector cannot sign.
    """
    await ensure_project_member(session, project_id, current_user)

    stmt = select(Item).where(Item.project_id == project_id)
    prefixes = await member_prefixes(session, project_id, current_user)
    if prefixes:
        stmt = stmt.where(path_scope_clause(Item.path, prefixes))
    if item_status is not None:
        stmt = stmt.where(Item.status == item_status.value)
    if media_type is not None:
        stmt = stmt.where(Item.media_type == ModelMediaType(media_type.value))
    if assignee_id is not None:
        # Imported locally: only this filter needs `Task`, and importing it at
        # module scope would suggest items.py owns task queries, which it does
        # not — `api/v1/tasks.py` does.
        from app.models import Task

        stmt = stmt.where(Item.id.in_(select(Task.item_id).where(Task.assignee_id == assignee_id)))
    if q:
        stmt = stmt.where(Item.path.contains(q))
    if tag:
        stmt = stmt.where(_has_tag(tag))

    result = await paginate(
        session,
        stmt,
        limit=page.limit,
        cursor=page.cursor,
        order_by=(Item.created_at, Item.id),
        key_of=lambda item: (item.created_at, item.id),
    )
    media_urls = await _signed_media_urls(session, result.items, settings)
    thumbnail_urls = await _signed_thumbnail_urls(session, project_id, result.items, settings)
    return Page[ItemRead](
        items=[
            ItemRead.model_validate(item).model_copy(
                update={
                    "media_url": media_urls.get(item.id),
                    "thumbnail_url": thumbnail_urls.get(item.id),
                }
            )
            for item in result.items
        ],
        next_cursor=result.next_cursor,
    )


@router.post(
    "/projects/{project_id}/items",
    response_model=ItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register an item under a project",
)
async def create_item(
    project_id: UUID,
    payload: ItemCreate,
    session: SessionDep,
    current_user: CurrentUserDep,
    response: Response,
) -> ItemRead:
    """Register a scanned object as an item.

    Idempotent on `(project_id, connector_id, path)`: source scans re-run and
    re-discover the same objects, so a duplicate is not an error. A duplicate
    returns the existing row unchanged with `200 OK`; a genuinely new item
    returns `201 Created`. Racing duplicate inserts (two scans finishing at
    once) are resolved the same way, by falling back to the existing row when
    the database's unique constraint rejects the second insert.
    """
    await ensure_project_member(session, project_id, current_user)

    existing = await session.scalar(
        select(Item).where(
            Item.project_id == project_id,
            Item.connector_id == payload.connector_id,
            Item.path == payload.path,
        )
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return ItemRead.model_validate(existing)

    item = Item(
        project_id=project_id,
        connector_id=payload.connector_id,
        path=payload.path,
        media_type=ModelMediaType(payload.media_type.value),
        etag=payload.etag,
        size_bytes=payload.size_bytes,
        width=payload.width,
        height=payload.height,
        meta=payload.meta,
    )
    session.add(item)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Item).where(
                Item.project_id == project_id,
                Item.connector_id == payload.connector_id,
                Item.path == payload.path,
            )
        )
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return ItemRead.model_validate(existing)

    await session.refresh(item)
    return ItemRead.model_validate(item)


async def _signed_media_url(session: AsyncSession, item: Item, settings: Settings) -> str | None:
    """Best-effort short-lived media URL for `item` (AUTH-6).

    Never raises: an item whose connector cannot sign — an unresolvable
    secret, or a phase-1 stub type (`s3`, `gcs`, `http`) that has no
    implementation yet — is still a valid item, just one the UI cannot preview
    right now, so this returns `None` rather than failing the whole request.
    """
    source = await _media_source(session, item)
    if source is None:
        return None
    connector = await session.get(Connector, source.connector_id)
    if connector is None:
        return None
    return await _sign_through(connector, source.path, settings)


async def _media_source(session: AsyncSession, item: Item) -> TextSource | None:
    """The object `media_url` signs: the item's own file, or the extracted text
    of a PDF in text mode (None until it is ready, CONTRACTS.md *PDF text mode*).
    """
    if pdf_text_meta(item) is None:
        return TextSource(item.connector_id, item.path)
    project = await session.get(Project, item.project_id)
    return text_source(item, project) if project is not None else None


async def _sign_through(
    connector: Connector, path: str, settings: Settings, *, internal: bool = False
) -> str | None:
    """Sign one path through an already-loaded connector row, or return `None`.

    `storage_for` resolves the credential reference and closes the connector
    afterwards; built per call, it would otherwise leak the SDK's HTTP session
    and its connection pool.
    """
    try:
        async with storage_for(connector) as storage:
            return await storage.signed_url(
                path, expires_in=settings.signed_url_ttl, internal=internal
            )
    except (ConnectorError, SecretResolutionError):
        return None


async def _signed_media_urls(
    session: AsyncSession, items: Sequence[Item], settings: Settings
) -> dict[UUID, str | None]:
    """Sign a whole page of items, opening each connector once.

    A grid of 100 items usually shares one or two connectors. Building one per
    item would resolve the same secret a hundred times and, for identity-based
    Azure signing, fetch a hundred user delegation keys.
    """
    urls: dict[UUID, str | None] = {}
    sources: dict[UUID, TextSource] = {}
    for item in items:
        source = await _media_source(session, item)
        if source is None:
            urls[item.id] = None
        else:
            sources[item.id] = source
    connector_ids = {source.connector_id for source in sources.values()}
    connectors = {
        connector.id: connector
        for connector in (
            await session.scalars(select(Connector).where(Connector.id.in_(connector_ids)))
        )
    }
    for connector_id in connector_ids:
        connector = connectors.get(connector_id)
        if connector is None:
            continue
        try:
            async with storage_for(connector) as storage:
                for item_id, source in sources.items():
                    if source.connector_id != connector_id:
                        continue
                    try:
                        urls[item_id] = await storage.signed_url(
                            source.path, expires_in=settings.signed_url_ttl
                        )
                    except (ConnectorError, SecretResolutionError):
                        urls[item_id] = None
        except (ConnectorError, SecretResolutionError):
            # The whole connector is unusable: every item on it is unpreviewable.
            continue
    return urls


async def _signed_thumbnail_urls(
    session: AsyncSession, project_id: UUID, items: Sequence[Item], settings: Settings
) -> dict[UUID, str | None]:
    """Sign the thumbnails a page of items has (IMG-8), through the project's cache connector.

    Thumbnails are derived data on the effective cache connector (SRC-6), not
    the source, so they are signed separately from `media_url`. Items without
    a thumbnail yet are simply absent from the result; a cache connector that
    cannot sign leaves every thumbnail `None`, the same best-effort rule as media.
    """
    with_thumbnail = [item for item in items if item.thumbnail_path]
    if not with_thumbnail:
        return {}
    project = await session.get(Project, project_id)
    if project is None or project.effective_cache_connector_id is None:
        return {}
    connector = await session.get(Connector, project.effective_cache_connector_id)
    if connector is None:
        return {}
    urls: dict[UUID, str | None] = {}
    try:
        async with storage_for(connector) as storage:
            for item in with_thumbnail:
                assert item.thumbnail_path is not None  # filtered above
                try:
                    urls[item.id] = await storage.signed_url(
                        item.thumbnail_path, expires_in=settings.signed_url_ttl
                    )
                except (ConnectorError, SecretResolutionError):
                    urls[item.id] = None
    except (ConnectorError, SecretResolutionError):
        return {}
    return urls


@router.get(
    "/items/{item_id}",
    response_model=ItemRead,
    summary="An item, with a short-lived signed media URL",
)
async def get_item(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> ItemRead:
    """Fetch one item and mint it a fresh signed media URL.

    The URL is built on every call, capped at `settings.signed_url_ttl`
    (AUTH-6 caps this at 15 minutes) — never a long-lived or cached one. When
    the connector cannot produce one, `media_url` is `None` instead of
    failing the request (see `_signed_media_url`).
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)

    media_url = await _signed_media_url(session, item, settings)
    thumbnail_urls = await _signed_thumbnail_urls(session, item.project_id, [item], settings)
    data = ItemRead.model_validate(item)
    return data.model_copy(
        update={"media_url": media_url, "thumbnail_url": thumbnail_urls.get(item.id)}
    )


@router.get(
    "/items/{item_id}/views",
    response_model=list[ItemView],
    summary="An item's companion views, signed (§5 multimodal)",
)
async def get_item_views(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> list[ItemView]:
    """The files listed in `meta.views`, each with a short-lived signed URL.

    Only paths inside the project's `source_prefix` are signed: a view is
    something the annotators of this project may see, never an arbitrary
    object elsewhere in the container. A view that cannot be signed comes
    back with `url: null`, like `media_url`.
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)
    project = await get_or_404(session, Project, item.project_id)
    raw = item.meta.get("views") if isinstance(item.meta, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    connector = await session.get(Connector, item.connector_id)
    prefix = project.source_prefix or ""
    # A folder-limited member (§4) sees only views inside their own folders.
    folders = await member_prefixes(session, item.project_id, current_user)
    views: list[ItemView] = []
    for view in raw:
        path = view.get("path") if isinstance(view, dict) else None
        if not isinstance(path, str) or not path.startswith(prefix) or ".." in path.split("/"):
            continue
        if not path_in_scope(path, folders):
            continue
        media_type = media_type_for(path)
        url = await _sign_through(connector, path, settings) if connector is not None else None
        views.append(
            ItemView(
                path=path,
                label=view.get("label"),
                media_type=SchemaMediaType(media_type.value) if media_type is not None else None,
                url=url,
            )
        )
    return views


@router.post(
    "/items/{item_id}/tiles/sign",
    response_model=TileSignResponse,
    summary="Signed read URLs for an item's DZI tiles (IMG-1)",
)
async def sign_tiles(
    item_id: UUID,
    payload: TileSignRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> TileSignResponse:
    """Sign one URL per `[level, col, row]` tile of the item's DZI pyramid.

    409 when the item has no `meta.tiles` yet; 422 for a level above
    `max_level` or a col / row outside that level's grid. Tiles are derived
    data on the *result* connector, same as a thumbnail (IMG-8).
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)

    tiles_meta = item.meta.get("tiles") if item.meta else None
    if not isinstance(tiles_meta, dict):
        raise ConflictError("This item has no tile pyramid yet.")

    for level, col, row in payload.tiles:
        try:
            validate_tile_coordinate(tiles_meta, level, col, row)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc

    project = await session.get(Project, item.project_id)
    if project is None or project.effective_cache_connector_id is None:
        raise ConflictError("The project has no cache or result connector to sign tiles from.")
    connector = await get_or_404(session, Connector, project.effective_cache_connector_id)

    suffix = str(tiles_meta.get("suffix", TILE_SUFFIX))
    urls: list[str] = []
    try:
        async with storage_for(connector) as storage:
            for level, col, row in payload.tiles:
                path = tile_blob_path(item.id, level, col, row, suffix)
                urls.append(await storage.signed_url(path, expires_in=settings.signed_url_ttl))
    except (ConnectorError, SecretResolutionError) as exc:
        raise ConflictError(f"Could not sign tiles on the result connector: {exc}") from exc

    return TileSignResponse(urls=urls, expires_in=settings.signed_url_ttl)


@router.post(
    "/items/{item_id}/interactive",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=InteractiveResult,
    summary="Turn one click or box into a polygon through a segment model (ML-7)",
)
async def interactive_segment(
    item_id: UUID,
    payload: InteractiveRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> InteractiveResult:
    """Proxy one interactive prompt to a `segment` model of the organisation.

    The browser never talks to the model itself: the endpoint may sit inside
    the compose network, and its credential (BYOM-3) must not leave the API.
    Nothing is persisted — the polygon comes back and the annotator adds it
    like any hand-drawn shape, so undo, class hotkeys and validation all work
    on it unchanged. Any failure on the model side is a 503, never a 500: the
    person just gets "the model did not answer" and keeps drawing by hand.
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)
    model = await get_or_404(
        session, Model, payload.model_id, organization_id=current_user.organization_id
    )
    if model.task is not ModelTask.SEGMENT:
        raise ConflictError(f"Model '{model.name}' is a {model.task} model, not a segment model.")

    connector = await session.get(Connector, item.connector_id)
    # The model fetches the image, not the browser: sign for the internal endpoint.
    url = (
        await _sign_through(connector, item.path, settings, internal=True)
        if connector is not None
        else None
    )
    if url is None:
        raise ModelUnavailableError("The item's storage cannot produce a URL for the model.")

    prompt = PredictItem(id=str(item.id), url=url, width=item.width or 0, height=item.height or 0)
    point = (payload.point.x, payload.point.y) if payload.point is not None else None
    try:
        async with await ModelClient.for_model(
            model.endpoint_url, model.identity_type, model.secret_ref, model.identity_config
        ) as client:
            polygon = await client.interactive(prompt, point=point, box=payload.box)
    except (ModelUnavailable, ModelRejected, SecretResolutionError) as exc:
        raise ModelUnavailableError(str(exc)) from exc
    return InteractiveResult(points=polygon.points, confidence=polygon.confidence)


@router.post(
    "/items/{item_id}/ocr",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=OcrResult,
    summary="Read the words of one page of a scanned PDF through an ocr model",
)
async def ocr_page(
    item_id: UUID,
    payload: OcrRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    settings: SettingsDep,
) -> OcrResult:
    """Proxy one page of a pdf item to an `ocr` model of the organisation.

    Like `/interactive`: the model's endpoint and credential stay behind the
    API, and nothing is persisted — the words go to the annotator, which uses
    them as it uses a text layer (boxes carry the words inside them). The
    text of a scan is thus never stored by the platform (ARC-3), only the
    `text` of the boxes a person keeps. Model failures are a 503.
    """
    item = await get_or_404(session, Item, item_id)
    await ensure_item_member(session, item, current_user)
    model = await get_or_404(
        session, Model, payload.model_id, organization_id=current_user.organization_id
    )
    if model.task is not ModelTask.OCR:
        raise ConflictError(f"Model '{model.name}' is a {model.task} model, not an ocr model.")
    if item.media_type is not ModelMediaType.PDF:
        raise ConflictError("OCR reads pdf items.")
    page_count = (item.meta or {}).get("page_count")
    if isinstance(page_count, int) and payload.page > page_count:
        raise ValidationFailedError(f"The document has {page_count} pages.")

    connector = await session.get(Connector, item.connector_id)
    url = (
        await _sign_through(connector, item.path, settings, internal=True)
        if connector is not None
        else None
    )
    if url is None:
        raise ModelUnavailableError("The item's storage cannot produce a URL for the model.")

    document = PredictItem(id=str(item.id), url=url, width=0, height=0, media_type="pdf")
    try:
        async with await ModelClient.for_model(
            model.endpoint_url, model.identity_type, model.secret_ref, model.identity_config
        ) as client:
            page = await client.ocr(document, payload.page)
    except (ModelUnavailable, ModelRejected, SecretResolutionError) as exc:
        raise ModelUnavailableError(str(exc)) from exc
    return OcrResult(
        page=page.page,
        width=page.width,
        height=page.height,
        engine=page.engine,
        words=[OcrWord(text=text, bbox=bbox) for text, bbox in page.words],
    )


@router.post(
    "/items/{item_id}/skip",
    response_model=ItemRead,
    summary="Skip an item, recording why",
)
async def skip_item(
    item_id: UUID,
    payload: SkipRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> ItemRead:
    """Move an item to `skipped` through the workflow machine (TOOL-6).

    `reason` is required and stored on the item (`meta.skip_reason`) so a
    reviewer or project owner can see why work stalled. The transition itself,
    including which roles and source states allow it and whether the project
    allows skipping at all (WF-1), is decided by `services/item_flow.py` —
    this router never compares or assigns a status by hand.
    """
    item = await get_or_404(session, Item, item_id)
    role = await ensure_item_member(session, item, current_user)

    current = item.status
    config = await load_workflow(session, item.project_id)
    await apply_skip(session, item=item, role=role, config=config, reason=payload.reason)
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="item.skip",
        target_type="item",
        target_id=item.id,
        before={"status": current.value},
        after={"status": item.status.value, "reason": payload.reason},
        ip=client_ip,
    )

    await session.commit()
    await session.refresh(item)
    return ItemRead.model_validate(item)


#: Roles allowed to act on other people's work in bulk (WF-8). Same set as
#: task assignment in `api/v1/tasks.py`.
_CAN_BULK = frozenset({"owner", "reviewer"})


@router.post(
    "/projects/{project_id}/items/bulk",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=BulkResult,
    summary="Assign, return, approve or tag many items at once",
)
async def bulk_update_items(
    project_id: UUID,
    payload: BulkRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> BulkResult:
    """Apply one action to a list of items (WF-8); owner or reviewer only.

    Items the action does not fit come back in `skipped` with a reason
    rather than failing the request. Approval goes through the same verdict
    path as `POST /annotations/{id}/review`, so self-review rules, tasks,
    notifications and per-annotation audit rows all apply. One `item.bulk`
    audit row records the request itself.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role not in _CAN_BULK:
        raise ForbiddenError("Only a project owner or reviewer may run bulk operations.")
    config = await load_workflow(session, project_id)

    result = await bulk_items(
        session,
        project_id=project_id,
        request=payload,
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        role=role,
        config=config,
        ip=client_ip,
        path_prefixes=await member_prefixes(session, project_id, current_user),
    )
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action=f"item.bulk.{payload.action}",
        target_type="project",
        target_id=project_id,
        after={
            "requested": len(payload.item_ids),
            "applied": result.applied,
            "skipped": len(result.skipped),
        },
        ip=client_ip,
    )
    await session.commit()
    return result


@router.post(
    "/items/{item_id}/split",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=SplitResponse,
    summary="Split an image item into region annotate tasks",
)
async def split_item_endpoint(
    item_id: UUID,
    payload: SplitRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> SplitResponse:
    """Split an image into a grid or explicit regions (IMG-6); owner / reviewer only.

    Cancels the item's live ordinary annotate task and opens one region task
    per region; see `services/splitting.py` for the 409 / 422 rules.
    """
    item = await get_or_404(session, Item, item_id)
    role = await ensure_item_member(session, item, current_user)
    if role not in _CAN_BULK:
        raise ForbiddenError("Only a project owner or reviewer may split an item.")
    config = await load_workflow(session, item.project_id)

    tasks = await apply_split(session, item=item, config=config, request=payload)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="item.split",
        target_type="item",
        target_id=item.id,
        after={"regions": len(tasks)},
        ip=client_ip,
    )
    await session.commit()
    for task in tasks:
        await session.refresh(task)
    return SplitResponse(tasks=[TaskRead.model_validate(task) for task in tasks])


__all__ = ["SkipRequest", "router"]
