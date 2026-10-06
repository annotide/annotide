"""Browser uploads to a project's source connector (§12 upload path).

The API never carries media: it mints one write-scoped signed URL per file
and the browser PUTs the bytes straight to the store (or, for local disk, to
the API's own proxy — the one exception, see `api/v1/storage.py`). After the
PUTs the browser queues a `scan_source` job, which registers the new objects
as items exactly like any other discovery (SRC-2), so nothing here touches
the `item` table.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.connectors.errors import ConnectorError, UnsupportedOperation
from app.models import Connector, ConnectorType, Project
from app.schemas import UploadFileSpec, UploadTarget
from app.services.storage import storage_for

_FALLBACK_CONTENT_TYPE = "application/octet-stream"

# Request headers the browser must send with the PUT, per store. Azure needs
# the blob type on a raw REST create; the local proxy needs nothing special.
_STORE_HEADERS: dict[ConnectorType, dict[str, str]] = {
    ConnectorType.AZURE_BLOB: {"x-ms-blob-type": "BlockBlob"},
}


class InvalidUploadPath(ValueError):  # noqa: N818 - names the rejected input, not an "error"
    """A requested path is absolute, escapes its folder, or is otherwise unusable."""


def normalize_upload_path(path: str) -> str:
    """Return `path` as a clean relative POSIX path, or raise `InvalidUploadPath`.

    Directory pickers hand over `webkitRelativePath` values like
    `photos/2024/a.jpg`; Windows drag-and-drop may use backslashes. Anything
    that could leave the folder — `..`, a leading `/`, a drive letter, a NUL
    byte — is rejected here so no connector has to defend against it.
    """
    candidate = path.replace("\\", "/").strip()
    if not candidate or "\x00" in candidate:
        raise InvalidUploadPath(f"invalid upload path: {path!r}")
    posix = PurePosixPath(candidate)
    if posix.is_absolute() or ":" in posix.parts[0]:
        raise InvalidUploadPath(f"upload path must be relative: {path!r}")
    parts = [part for part in posix.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise InvalidUploadPath(f"upload path may not leave its folder: {path!r}")
    return "/".join(parts)


def join_prefix(prefix: str | None, relative: str) -> str:
    """`source_prefix` + a relative path, with exactly one separator between them."""
    clean = (prefix or "").strip("/")
    return f"{clean}/{relative}" if clean else relative


async def mint_upload_targets(
    project: Project, connector: Connector, files: list[UploadFileSpec], *, expires_in: int
) -> list[UploadTarget]:
    """One write-scoped signed URL per file, all on the project's source connector.

    Raises `InvalidUploadPath` for a bad path, `UnsupportedOperation` when the
    connector cannot sign writes (read-only `http`), and `ConnectorError` for anything
    the store itself refuses. Paths are normalised *before* any URL is
    minted so a single bad entry fails the whole batch and nothing is
    half-authorised.
    """
    paths = [join_prefix(project.source_prefix, normalize_upload_path(spec.path)) for spec in files]
    if len(set(paths)) != len(paths):
        raise InvalidUploadPath("duplicate upload paths in one request")

    store_headers = _STORE_HEADERS.get(connector.type, {})
    targets: list[UploadTarget] = []
    async with storage_for(connector) as storage:
        for spec, path in zip(files, paths, strict=True):
            try:
                url = await storage.signed_url(path, expires_in=expires_in, write=True)
            except UnsupportedOperation:
                raise
            except ConnectorError as exc:
                # A traversal the connector still catches after our own
                # normalisation is a bad path, not a store failure.
                raise InvalidUploadPath(str(exc)) from exc
            headers = {
                **store_headers,
                "Content-Type": spec.content_type or _FALLBACK_CONTENT_TYPE,
            }
            targets.append(UploadTarget(path=path, url=url, method="PUT", headers=headers))
    return targets
