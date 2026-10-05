"""Media proxy for the `local` connector (AUTH-6, SEC-4).

Object stores sign their own URLs; local disk cannot, so `LocalConnector`
mints a URL that points back here and this route serves the bytes. It is the
one place where the API touches media, and it exists only because local disk
gives it no alternative — every other connector hands the browser a URL to
its own store.

The route is unauthenticated on purpose. An `<img>` tag sends no
`Authorization` header and, cross-origin, no cookies either, so the query
string has to carry its own proof: an expiry plus an HMAC over
(connector id, object path, expiry). Without a valid signature there is no
way in, and a signature is good for one object until it expires.
"""

from __future__ import annotations

import mimetypes
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response, status

from app.api.deps import SessionDep
from app.api.errors import (
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from app.connectors.base import SizedConnector
from app.connectors.errors import ConnectorError, ConnectorNotFound
from app.core.security import verify_storage_path
from app.models import Connector, ConnectorType
from app.services.secrets import SecretResolutionError
from app.services.storage import storage_for

router = APIRouter(tags=["storage"])

_FALLBACK_CONTENT_TYPE = "application/octet-stream"

#: Types the annotator renders inline (`<img>`, `<video>`, `<audio>`, pdf.js,
#: text and JSON previews). Everything else, notably `text/html`, `image/svg+xml`
#: and `application/xhtml+xml`, can carry script and is forced to download.
_INLINE_SAFE_EXACT = frozenset({"application/pdf", "application/json", "text/plain", "text/csv"})


def _is_inline_safe(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    if base == "image/svg+xml":
        return False
    return base in _INLINE_SAFE_EXACT or base.startswith(("image/", "video/", "audio/"))


# Upper bound for one proxied upload. Object stores take the bytes directly
# from the browser; only local disk goes through the API, and the API holds
# the body in memory for the duration of the request.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024


#: Connectors whose media the API serves itself: no native signing (ARC-3 exception).
PROXIED_TYPES = frozenset({ConnectorType.LOCAL, ConnectorType.DATABRICKS_VOLUME})

#: Most bytes one `Range` response carries. A media element asks for
#: `bytes=0-` and then seeks; capping the answer keeps the API from reading a
#: whole video into memory, and the browser asks again for the rest.
MAX_RANGE_BYTES = 8 * 1024 * 1024


class _RangeNotSatisfiableError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"range outside 0-{size}")
        self.size = size


def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The `[start, end)` one `Range: bytes=...` header asks of a `size`-byte object.

    None means serve the whole object: a malformed header, a unit other than
    bytes, or several ranges (which media elements never send) are ignored,
    as RFC 9110 allows. A range starting past the end raises
    `_RangeNotSatisfiableError` (416). The answer is capped at
    `MAX_RANGE_BYTES`.
    """
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = (part.strip() for part in spec.partition("-"))
    # Digits only: int() would also take signs, spaces and underscores.
    if not dash or not (first or last) or not all(p.isdigit() for p in (first, last) if p):
        return None
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise _RangeNotSatisfiableError(size)
        start, end = max(size - suffix, 0), size
    else:
        start = int(first)
        end = int(last) + 1 if last else size
        if start >= size:
            raise _RangeNotSatisfiableError(size)
        if end <= start:
            return None
    return start, min(end, size, start + MAX_RANGE_BYTES)


@router.get(
    "/storage/proxy/{connector_id}/{path:path}",
    summary="Serve one object from a proxied connector against a signed URL",
    response_class=Response,
    include_in_schema=False,
)
@router.get(
    "/storage/local/{connector_id}/{path:path}",
    summary="Serve one object from a local connector against a signed URL",
    response_class=Response,
    responses={
        200: {"content": {"*/*": {}}, "description": "The object's bytes"},
        206: {"content": {"*/*": {}}, "description": "The bytes a `Range` header asked for"},
        403: {"description": "Missing, malformed or expired signature"},
        404: {"description": "No such local connector, or no such object"},
        416: {"description": "The range starts past the end of the object"},
    },
)
async def get_local_object(
    request: Request,
    session: SessionDep,
    connector_id: Annotated[UUID, Path(description="The local connector that owns the object")],
    path: Annotated[str, Path(description="Object path, relative to the connector root")],
    expires: Annotated[int, Query(description="Absolute Unix timestamp the signature dies at")],
    sig: Annotated[str, Query(description="HMAC over connector id, path and expiry")],
) -> Response:
    """Return one object's bytes if the signature covers it and is still live.

    Signature first, database second: an unsigned request must not be able to
    probe which connector ids exist. Expiry and signature failures are both
    403 with the same body, so a caller cannot tell a forged signature from a
    stale one.

    A `Range: bytes=...` header is answered with 206 and `Content-Range`, so
    audio and video can seek without the whole file passing through the API.
    """
    if not verify_storage_path(str(connector_id), path, expires, sig) or expires < int(time.time()):
        raise ForbiddenError("This media URL is not valid or has expired.")

    connector = await session.get(Connector, connector_id)
    if connector is None or connector.type not in PROXIED_TYPES:
        raise NotFoundError("No such proxied connector.")

    range_header = request.headers.get("range")
    span: tuple[int, int] | None = None
    size = 0
    try:
        async with storage_for(connector) as storage:
            if range_header and isinstance(storage, SizedConnector):
                size = await storage.size(path)
                span = byte_range(range_header, size)
            if span is None:
                data = await storage.read(path)
            else:
                data = await storage.read(path, span[0], span[1])
    except _RangeNotSatisfiableError as exc:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{exc.size}", "Accept-Ranges": "bytes"},
        )
    except ConnectorNotFound as exc:
        raise NotFoundError("No such object.") from exc
    except (ConnectorError, SecretResolutionError) as exc:
        # A signed URL for an unreadable object is a missing object as far as
        # the browser is concerned; the cause is already in the server logs.
        raise NotFoundError("No such object.") from exc

    content_type = mimetypes.guess_type(path)[0] or _FALLBACK_CONTENT_TYPE
    headers = {
        # The object is customer-uploaded and served from the API origin:
        # keep any markup in it inert (see `_is_inline_safe`).
        "Content-Security-Policy": "sandbox; default-src 'none'",
        # Cacheable for as long as the signature lives, and never by a
        # shared cache: the URL is a credential.
        "Cache-Control": f"private, max-age={max(0, expires - int(time.time()))}",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    if content_type == "application/pdf":
        # A sandboxed document cannot host the browser's PDF viewer, so an
        # "Open" link would show a blank page; the viewer runs no script
        # with this origin's rights anyway.
        del headers["Content-Security-Policy"]
    if not _is_inline_safe(content_type):
        headers["Content-Disposition"] = "attachment"
    if span is None:
        return Response(content=data, media_type=content_type, headers=headers)
    headers["Content-Range"] = f"bytes {span[0]}-{span[0] + len(data) - 1}/{size}"
    return Response(
        content=data,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers=headers,
    )


@router.put(
    "/storage/proxy/{connector_id}/{path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
@router.put(
    "/storage/local/{connector_id}/{path:path}",
    summary="Store one object on a local connector against a write-scoped signed URL",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Stored"},
        403: {"description": "Missing, malformed, expired or read-only signature"},
        404: {"description": "No such local connector"},
        413: {"description": "Body over MAX_UPLOAD_BYTES"},
    },
)
async def put_local_object(
    request: Request,
    session: SessionDep,
    connector_id: Annotated[UUID, Path(description="The local connector that owns the object")],
    path: Annotated[str, Path(description="Object path, relative to the connector root")],
    expires: Annotated[int, Query(description="Absolute Unix timestamp the signature dies at")],
    sig: Annotated[str, Query(description="Write-scoped HMAC over connector id, path and expiry")],
) -> Response:
    """Write the request body as the object the signature names.

    The write half of the proxy, minted by `POST /projects/{id}/uploads` for
    a `local` source connector. The signature is checked with the write
    scope, so a read URL (which any item listing hands out) cannot be
    replayed here. Same ordering as the read route: signature before any
    database lookup.
    """
    if not verify_storage_path(str(connector_id), path, expires, sig, write=True) or expires < int(
        time.time()
    ):
        raise ForbiddenError("This upload URL is not valid or has expired.")

    connector = await session.get(Connector, connector_id)
    if connector is None or connector.type not in PROXIED_TYPES:
        raise NotFoundError("No such proxied connector.")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise PayloadTooLargeError(f"Uploads are limited to {MAX_UPLOAD_BYTES} bytes.")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_UPLOAD_BYTES:
            raise PayloadTooLargeError(f"Uploads are limited to {MAX_UPLOAD_BYTES} bytes.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise ValidationFailedError("The uploaded body is empty.")

    content_type = request.headers.get("content-type") or (
        mimetypes.guess_type(path)[0] or _FALLBACK_CONTENT_TYPE
    )
    try:
        async with storage_for(connector) as storage:
            await storage.write(path, data, content_type)
    except (ConnectorError, SecretResolutionError) as exc:
        raise NotFoundError("No such object.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
