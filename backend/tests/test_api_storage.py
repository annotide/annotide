"""Tests for `api/v1/storage.py` — the local connector's media proxy.

The route is the one unauthenticated endpoint in the API: an `<img>` tag
cannot send a bearer token, so a signature in the query string is the whole
authorisation story. These tests are mostly about what it refuses.

Same harness as `test_api_items.py`: in-memory SQLite through
`dependency_overrides`, no `user` table (it needs PostgreSQL's `CITEXT`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import storage as storage_api
from app.api.v1.storage import byte_range
from app.core.security import sign_storage_path
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Organization,
)

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, Connector.__table__),
]

_PIXEL = b"\x89PNG\r\n\x1a\n" + b"fake-but-recognisable"


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


async def _seed_connector(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    root: Path,
    connector_type: ConnectorType = ConnectorType.LOCAL,
) -> UUID:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        connector = Connector(
            organization_id=org.id,
            name="source",
            type=connector_type,
            identity_type=ConnectorIdentity.NONE,
            config={"root": str(root)},
        )
        session.add(connector)
        await session.commit()
        await session.refresh(connector)
        return connector.id


def _url(connector_id: UUID, path: str, *, ttl: int = 900) -> str:
    expires = int(time.time()) + ttl
    sig = sign_storage_path(str(connector_id), path, expires)
    return f"/api/v1/storage/local/{connector_id}/{path}?expires={expires}&sig={sig}"


class TestSignedRead:
    async def test_serves_the_object(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.png"))

        assert response.status_code == 200
        assert response.content == _PIXEL
        assert response.headers["content-type"].startswith("image/png")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"].startswith("private")

    async def test_advertises_byte_ranges(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        assert client.get(_url(connector_id, "a.png")).headers["accept-ranges"] == "bytes"

    @pytest.mark.parametrize(
        ("header", "start", "end"),
        [("bytes=0-3", 0, 4), ("bytes=4-", 4, len(_PIXEL)), ("bytes=-5", len(_PIXEL) - 5, None)],
    )
    async def test_serves_a_byte_range(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
        header: str,
        start: int,
        end: int | None,
    ) -> None:
        (tmp_path / "a.wav").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.wav"), headers={"Range": header})

        expected = _PIXEL[start:end]
        assert response.status_code == 206
        assert response.content == expected
        last = start + len(expected) - 1
        assert response.headers["content-range"] == f"bytes {start}-{last}/{len(_PIXEL)}"
        assert response.headers["cache-control"].startswith("private")

    async def test_an_open_range_is_capped(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(storage_api, "MAX_RANGE_BYTES", 10)
        (tmp_path / "a.mp4").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.mp4"), headers={"Range": "bytes=0-"})

        assert response.status_code == 206
        assert response.content == _PIXEL[:10]
        assert response.headers["content-range"] == f"bytes 0-9/{len(_PIXEL)}"

    async def test_a_range_past_the_end_is_416(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.png"), headers={"Range": "bytes=999-"})

        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(_PIXEL)}"

    async def test_a_range_on_a_missing_object_is_404(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "gone.wav"), headers={"Range": "bytes=0-"})

        assert response.status_code == 404

    async def test_serves_a_nested_path(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "images/a.png"))

        assert response.status_code == 200
        assert response.content == _PIXEL

    async def test_round_trips_the_url_the_connector_mints(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        """The contract that broke before: what `signed_url` returns must resolve."""
        from app.models import Connector as ConnectorModel
        from app.services.storage import storage_for

        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        async with sessionmaker() as session:
            connector = await session.get(ConnectorModel, connector_id)
            assert connector is not None
            async with storage_for(connector) as storage:
                url = await storage.signed_url("a.png", expires_in=900)

        response = client.get(url)

        assert response.status_code == 200
        assert response.content == _PIXEL


class TestActiveContent:
    async def test_media_stays_inline_but_sandboxed(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.png"))

        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in response.headers

    async def test_pdf_opens_in_the_browser_viewer(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\n")
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.pdf"))

        assert response.status_code == 200
        assert "content-security-policy" not in response.headers
        assert "content-disposition" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("name", ["x.html", "x.svg", "x.xhtml"])
    async def test_script_capable_types_download(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
        name: str,
    ) -> None:
        (tmp_path / name).write_bytes(b"<script>alert(1)</script>")
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, name))

        assert response.status_code == 200
        assert response.headers["content-disposition"] == "attachment"
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_ranged_media_keeps_range_and_cache_headers(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.png"), headers={"Range": "bytes=0-3"})

        assert response.status_code == 206
        assert response.headers["content-range"].startswith("bytes 0-3/")
        assert response.headers["cache-control"].startswith("private")
        assert "content-disposition" not in response.headers


class TestRefusals:
    async def test_rejects_a_non_ascii_signature(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        expires = int(time.time()) + 900

        response = client.get(
            f"/api/v1/storage/local/{connector_id}/a.png?expires={expires}&sig=%C3%A9%E2%82%AC"
        )

        assert response.status_code == 403

    async def test_rejects_a_forged_signature(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        expires = int(time.time()) + 900

        response = client.get(
            f"/api/v1/storage/local/{connector_id}/a.png?expires={expires}&sig=not-a-signature"
        )

        assert response.status_code == 403

    async def test_rejects_a_signature_for_another_path(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        """A URL for one object must not become a URL for its neighbour."""
        (tmp_path / "public.png").write_bytes(_PIXEL)
        (tmp_path / "private.png").write_bytes(b"secret")
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        query = parse_qs(urlparse(_url(connector_id, "public.png")).query)

        response = client.get(
            f"/api/v1/storage/local/{connector_id}/private.png"
            f"?expires={query['expires'][0]}&sig={query['sig'][0]}"
        )

        assert response.status_code == 403

    async def test_rejects_a_signature_from_another_connector(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        other = await _seed_connector(sessionmaker, root=tmp_path)
        query = parse_qs(urlparse(_url(other, "a.png")).query)

        response = client.get(
            f"/api/v1/storage/local/{connector_id}/a.png"
            f"?expires={query['expires'][0]}&sig={query['sig'][0]}"
        )

        assert response.status_code == 403

    async def test_rejects_an_expired_signature(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "a.png", ttl=-10))

        assert response.status_code == 403

    async def test_requires_the_query_parameters(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(f"/api/v1/storage/local/{connector_id}/a.png")

        assert response.status_code == 422

    async def test_traversal_is_refused_even_when_signed(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        """SEC-4: a validly signed `..` must still not escape the root.

        The signature proves the URL came from us; it does not make the path
        safe, so the connector re-validates it on the way in.
        """
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "outside.txt").write_bytes(b"secret")
        connector_id = await _seed_connector(sessionmaker, root=root)
        # Percent-encoded so the HTTP client does not collapse the `..` away
        # before the request leaves; the server must be the one to refuse it.
        expires = int(time.time()) + 900
        sig = sign_storage_path(str(connector_id), "../outside.txt", expires)

        response = client.get(
            f"/api/v1/storage/local/{connector_id}/%2E%2E/outside.txt?expires={expires}&sig={sig}"
        )

        assert response.status_code == 404
        assert b"secret" not in response.content

    async def test_unknown_connector_is_404(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        missing = uuid4()

        response = client.get(_url(missing, "a.png"))

        assert response.status_code == 404

    async def test_non_local_connector_is_404(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        """The proxy exists for local disk only; other types sign themselves."""
        connector_id = await _seed_connector(
            sessionmaker, root=tmp_path, connector_type=ConnectorType.S3
        )

        response = client.get(_url(connector_id, "a.png"))

        assert response.status_code == 404

    async def test_missing_object_is_404(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_url(connector_id, "gone.png"))

        assert response.status_code == 404


def _put_url(connector_id: UUID, path: str, *, ttl: int = 900, write: bool = True) -> str:
    expires = int(time.time()) + ttl
    sig = sign_storage_path(str(connector_id), path, expires, write=write)
    return f"/api/v1/storage/local/{connector_id}/{path}?expires={expires}&sig={sig}"


class TestSignedWrite:
    async def test_stores_the_body(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.put(
            _put_url(connector_id, "incoming/a.png"),
            content=_PIXEL,
            headers={"Content-Type": "image/png"},
        )

        assert response.status_code == 204
        assert (tmp_path / "incoming" / "a.png").read_bytes() == _PIXEL

    async def test_round_trips_the_write_url_the_connector_mints(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        from app.models import Connector as ConnectorModel
        from app.services.storage import storage_for

        connector_id = await _seed_connector(sessionmaker, root=tmp_path)
        async with sessionmaker() as session:
            connector = await session.get(ConnectorModel, connector_id)
            assert connector is not None
            async with storage_for(connector) as storage:
                url = await storage.signed_url("b.png", expires_in=900, write=True)

        response = client.put(url, content=_PIXEL)

        assert response.status_code == 204
        assert (tmp_path / "b.png").read_bytes() == _PIXEL

    async def test_read_signature_cannot_write(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        """Every item listing hands out read URLs; none of them may overwrite media."""
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.put(_put_url(connector_id, "a.png", write=False), content=b"evil")

        assert response.status_code == 403
        assert (tmp_path / "a.png").read_bytes() == _PIXEL

    async def test_write_signature_cannot_read(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        (tmp_path / "a.png").write_bytes(_PIXEL)
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.get(_put_url(connector_id, "a.png", write=True))

        assert response.status_code == 403

    async def test_rejects_an_expired_signature(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.put(_put_url(connector_id, "a.png", ttl=-1), content=_PIXEL)

        assert response.status_code == 403
        assert not (tmp_path / "a.png").exists()

    async def test_rejects_an_empty_body(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.put(_put_url(connector_id, "a.png"), content=b"")

        assert response.status_code == 422
        assert not (tmp_path / "a.png").exists()

    async def test_rejects_an_oversized_declared_length(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        from app.api.v1.storage import MAX_UPLOAD_BYTES

        connector_id = await _seed_connector(sessionmaker, root=tmp_path)

        response = client.put(
            _put_url(connector_id, "a.png"),
            content=b"x",
            headers={"Content-Length": str(MAX_UPLOAD_BYTES + 1)},
        )

        assert response.status_code == 413

    async def test_traversal_is_refused_even_when_signed(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        connector_id = await _seed_connector(sessionmaker, root=root)

        expires = int(time.time()) + 900
        sig = sign_storage_path(str(connector_id), "../escape.png", expires, write=True)

        response = client.put(
            f"/api/v1/storage/local/{connector_id}/%2E%2E/escape.png?expires={expires}&sig={sig}",
            content=_PIXEL,
        )

        assert response.status_code == 404
        assert not (tmp_path / "escape.png").exists()

    async def test_non_local_connector_is_404(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Any,
    ) -> None:
        connector_id = await _seed_connector(
            sessionmaker, root=tmp_path, connector_type=ConnectorType.S3
        )

        response = client.put(_put_url(connector_id, "a.png"), content=_PIXEL)

        assert response.status_code == 404


class TestByteRange:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("bytes=0-0", (0, 1)),
            ("bytes=10-19", (10, 20)),
            ("bytes=90-200", (90, 100)),
            ("bytes=95-", (95, 100)),
            ("bytes=-10", (90, 100)),
            ("bytes=-500", (0, 100)),
            ("BYTES = 5-6", (5, 7)),
            ("bytes=0-1,5-6", None),
            ("items=0-1", None),
            ("bytes=5-2", None),
            ("bytes=a-b", None),
            ("bytes=5", None),
            ("bytes=--5", None),
        ],
    )
    def test_parses(self, header: str, expected: tuple[int, int] | None) -> None:
        assert byte_range(header, 100) == expected

    @pytest.mark.parametrize("header", ["bytes=100-", "bytes=500-600", "bytes=-0"])
    def test_unsatisfiable(self, header: str) -> None:
        with pytest.raises(storage_api._RangeNotSatisfiableError):
            byte_range(header, 100)

    def test_an_empty_object_satisfies_no_range(self) -> None:
        with pytest.raises(storage_api._RangeNotSatisfiableError):
            byte_range("bytes=0-", 0)

    def test_caps_at_max_range_bytes(self) -> None:
        size = storage_api.MAX_RANGE_BYTES * 3
        assert byte_range("bytes=0-", size) == (0, storage_api.MAX_RANGE_BYTES)
