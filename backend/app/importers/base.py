"""Importer protocol, shared data structures and the format registry (EXP-6).

An importer is a pure parser: it turns the files of one dataset into
:class:`ImportedItem` records that still carry the *source's* class names and
coordinate convention. Matching records to project items, mapping classes to
the label schema and validating the result is the import job's business
(`app.worker.jobs`), so every importer stays free of database and schema
knowledge and can be unit-tested on bytes alone.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, ClassVar, Literal, Protocol

Coordinates = Literal["pixel", "normalized"]


class ImportFormatError(ValueError):
    """The input is not a well-formed file of the requested format.

    A `ValueError`, so the worker treats it as permanent: a malformed file
    does not get better by retrying.
    """


@dataclass(frozen=True, slots=True)
class ImportFile:
    """One input file, with its path relative to the upload or archive root."""

    path: str
    data: bytes

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def stem(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix.lower()


@dataclass(slots=True)
class ImportedItem:
    """One media file's annotations as the source describes them.

    `path` is whatever the source calls the file: a relative path, a bare file
    name or (YOLO) just the stem — the job resolves it against project items.
    `shapes` are JSON-shaped `Shape` dicts (`type`, `class`, `bbox` / `points`
    / `point` / `rle` / `start`+`end` / `from`+`to`, optional `attributes`,
    `frame`/`track_id`/`keyframe`/`outside` for video) usually without an
    `id`; the job assigns ids, maps classes and validates. With
    `coordinates == "normalized"` every coordinate is a `[0, 1]` fraction of
    the image width / height. `media_type`, when known, is checked against the
    matched item's own media type (EXP-6); `meta` carries source metadata such
    as `frame_count`/`fps` for the job to attach to the matched item.
    """

    path: str
    width: int | None = None
    height: int | None = None
    coordinates: Coordinates = "pixel"
    shapes: list[dict[str, Any]] = field(default_factory=list)
    classification: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    media_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Importer(Protocol):
    """A format-specific parser from dataset files to imported items."""

    format: ClassVar[str]

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        """Parse `files` into one record per media file the source mentions.

        Raises :class:`ImportFormatError` when the input is not this format.
        Files the format does not use (images, READMEs) are ignored.
        """
        ...


IMPORTERS: dict[str, Importer] = {}


def register_importer(importer_cls: type[Importer]) -> type[Importer]:
    """Class decorator: instantiate `importer_cls` and register it under its `format`."""
    IMPORTERS[importer_cls.format] = importer_cls()
    return importer_cls


def get_importer(format: str) -> Importer:  # noqa: A002 - matches the contract's parameter name
    """Look up a registered importer by format name."""
    try:
        return IMPORTERS[format]
    except KeyError as exc:
        raise KeyError(f"unknown import format: '{format}'") from exc


#: Zip bomb limits (SEC): the upload cap bounds the compressed bytes only. The total
#: is 8x the import upload cap (`MAX_IMPORT_BYTES`, 64 MiB; importers sit below
#: the services layer, so the number is repeated here).
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 8 * 64 * 1024 * 1024


def expand_archive(
    data: bytes,
    *,
    max_members: int = MAX_ARCHIVE_MEMBERS,
    max_uncompressed_bytes: int = MAX_ARCHIVE_UNCOMPRESSED_BYTES,
) -> list[ImportFile]:
    """Unpack a zip archive into import files, dropping directories and macOS cruft.

    Refuses an archive with more than `max_members` entries or whose declared
    uncompressed sizes add up to more than `max_uncompressed_bytes`; each member
    is also read through a bound, so a header that understates its size cannot
    smuggle a bomb through.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportFormatError("the upload is not a valid zip archive") from exc
    infos = archive.infolist()
    if len(infos) > max_members:
        raise ImportFormatError(f"the archive has more than {max_members} entries")
    if sum(info.file_size for info in infos) > max_uncompressed_bytes:
        raise ImportFormatError(
            f"the archive expands to more than {max_uncompressed_bytes} bytes uncompressed"
        )
    files: list[ImportFile] = []
    remaining = max_uncompressed_bytes
    for info in infos:
        if info.is_dir():
            continue
        path = PurePosixPath(info.filename)
        if any(part in {"__MACOSX", ".DS_Store"} or part.startswith("._") for part in path.parts):
            continue
        try:
            with archive.open(info) as member:
                content = member.read(remaining + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
            raise ImportFormatError(f"cannot read '{info.filename}' from the archive") from exc
        remaining -= len(content)
        if remaining < 0:
            raise ImportFormatError(
                f"the archive expands to more than {max_uncompressed_bytes} bytes uncompressed"
            )
        files.append(ImportFile(path=str(path), data=content))
    return files


def is_archive(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() == ".zip"
