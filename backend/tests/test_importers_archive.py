"""`expand_archive` limits: member count and uncompressed size (zip bombs)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.importers import ImportFormatError, expand_archive
from app.importers.base import MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_UNCOMPRESSED_BYTES


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_a_normal_archive_is_unpacked() -> None:
    data = _zip({"a.txt": b"one", "sub/b.txt": b"two", "__MACOSX/._a.txt": b"x", "sub/": b""})
    files = expand_archive(data)
    assert {f.path: f.data for f in files} == {"a.txt": b"one", "sub/b.txt": b"two"}


def test_not_a_zip_is_a_format_error() -> None:
    with pytest.raises(ImportFormatError, match="not a valid zip"):
        expand_archive(b"nope")


def test_too_many_members_are_refused() -> None:
    data = _zip({f"{i}.txt": b"x" for i in range(5)})
    with pytest.raises(ImportFormatError, match="more than 4 entries"):
        expand_archive(data, max_members=4)
    assert len(expand_archive(data, max_members=5)) == 5


def test_an_oversized_declared_total_is_refused_before_reading() -> None:
    # 1 MiB of zeros deflates to about a kilobyte: the compressed upload is tiny.
    data = _zip({"a.bin": bytes(1024 * 1024), "b.bin": bytes(1024 * 1024)})
    assert len(data) < 10_000
    with pytest.raises(ImportFormatError, match="uncompressed"):
        expand_archive(data, max_uncompressed_bytes=2 * 1024 * 1024 - 1)
    assert len(expand_archive(data, max_uncompressed_bytes=2 * 1024 * 1024)) == 2


def test_the_defaults_are_the_named_limits() -> None:
    assert expand_archive.__kwdefaults__ == {
        "max_members": MAX_ARCHIVE_MEMBERS,
        "max_uncompressed_bytes": MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    }
    assert MAX_ARCHIVE_MEMBERS == 100_000
    assert MAX_ARCHIVE_UNCOMPRESSED_BYTES == 8 * 64 * 1024 * 1024


def test_a_header_that_lies_about_the_size_cannot_bypass_the_bound() -> None:
    data = bytearray(_zip({"a.bin": bytes(100_000)}))
    # Understate the uncompressed size in both the local and the central header.
    real = (100_000).to_bytes(4, "little")
    fake = (10).to_bytes(4, "little")
    for _ in range(2):
        index = data.index(real)
        data[index : index + 4] = fake
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        assert archive.infolist()[0].file_size == 10
    # zipfile itself stops at the declared size and fails the CRC: refused, not read.
    with pytest.raises(ImportFormatError, match="cannot read"):
        expand_archive(bytes(data), max_uncompressed_bytes=50_000)


def test_the_read_bound_is_enforced_when_the_declared_sizes_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Declared sizes sum under the cap, but the members stream more than they declare.
    data = _zip({"a.bin": b"x", "b.bin": b"x"})
    real = zipfile.ZipFile.infolist

    def understate(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = real(self)
        for info in infos:
            info.file_size = 10
        return infos

    def stream_more(self: zipfile.ZipFile, info: zipfile.ZipInfo) -> io.BytesIO:
        return io.BytesIO(bytes(1000))

    monkeypatch.setattr(zipfile.ZipFile, "infolist", understate)
    monkeypatch.setattr(zipfile.ZipFile, "open", stream_more)
    with pytest.raises(ImportFormatError, match="uncompressed"):
        expand_archive(data, max_uncompressed_bytes=1500)
