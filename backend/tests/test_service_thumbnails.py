"""Tests for `services/thumbnails.py` (IMG-8): rendering and item selection."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from PIL import Image
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import Connector, ConnectorIdentity, ConnectorType, Item, MediaType, Project
from app.services.thumbnails import (
    ThumbnailError,
    render_pdf_thumbnail,
    render_thumbnail,
    select_items,
    thumbnail_blob_path,
)
from tests.support import pdf, png

ORG_ID = uuid.uuid4()


def _encode(image: Image.Image, fmt: str, **kwargs: object) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


class TestRenderThumbnail:
    def test_downscales_to_the_longest_side_and_encodes_jpeg(self) -> None:
        source = _encode(Image.new("RGB", (800, 200), (200, 30, 30)), "PNG")

        out = render_thumbnail(source, 100)

        with Image.open(io.BytesIO(out)) as result:
            assert result.format == "JPEG"
            assert result.size == (100, 25)
            assert result.mode == "RGB"

    def test_never_upscales_a_small_image(self) -> None:
        source = png(4, 3, bytearray(4 * 3 * 3))

        with Image.open(io.BytesIO(render_thumbnail(source, 256))) as result:
            assert result.size == (4, 3)

    def test_flattens_transparency_onto_white(self) -> None:
        source = _encode(Image.new("RGBA", (10, 10), (0, 0, 0, 0)), "PNG")

        with Image.open(io.BytesIO(render_thumbnail(source, 10))) as result:
            assert result.getpixel((5, 5)) == (255, 255, 255)

    def test_applies_exif_orientation(self) -> None:
        image = Image.new("RGB", (40, 20))
        exif = image.getexif()
        exif[0x0112] = 6  # rotate 90° clockwise
        source = _encode(image, "JPEG", exif=exif.tobytes())

        with Image.open(io.BytesIO(render_thumbnail(source, 40))) as result:
            assert result.size == (20, 40)

    def test_jpeg_draft_mode_survives_large_sources(self) -> None:
        source = _encode(Image.new("RGB", (2000, 1500), (10, 200, 10)), "JPEG")

        with Image.open(io.BytesIO(render_thumbnail(source, 256))) as result:
            assert result.size == (256, 192)

    def test_rejects_bytes_that_are_not_an_image(self) -> None:
        with pytest.raises(ThumbnailError):
            render_thumbnail(b"definitely not an image", 256)

    def test_rejects_a_truncated_image(self) -> None:
        source = _encode(Image.new("RGB", (300, 300)), "JPEG")

        with pytest.raises(ThumbnailError):
            render_thumbnail(source[: len(source) // 3], 64)

    def test_rejects_a_nonpositive_size(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            render_thumbnail(png(1, 1, bytearray(3)), 0)

    def test_blob_path_is_under_the_cache_prefix(self) -> None:
        item_id = uuid.uuid4()
        assert thumbnail_blob_path(item_id) == f"cache/thumbnails/{item_id}.jpg"


_TABLES = cast("list[Table]", [Connector.__table__, Project.__table__, Item.__table__])


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, list[Item]]:
    async with sessionmaker() as session:
        connector = Connector(
            organization_id=ORG_ID,
            name="local",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp/fixture"},
        )
        session.add(connector)
        await session.flush()
        project = Project(organization_id=ORG_ID, name="proj")
        other = Project(organization_id=ORG_ID, name="other")
        session.add_all([project, other])
        await session.flush()

        def item(
            path: str, media_type: MediaType, project_id: uuid.UUID, thumb: str | None
        ) -> Item:
            return Item(
                project_id=project_id,
                connector_id=connector.id,
                path=path,
                media_type=media_type,
                size_bytes=1,
                meta={},
                thumbnail_path=thumb,
            )

        rows = [
            item("a.png", MediaType.IMAGE, project.id, None),
            item("b.png", MediaType.IMAGE, project.id, "cache/thumbnails/b.jpg"),
            item("notes.txt", MediaType.TEXT, project.id, None),
            item("c.png", MediaType.IMAGE, other.id, None),
        ]
        session.add_all(rows)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        return project.id, rows


class TestSelectItems:
    async def test_selects_images_without_a_thumbnail_in_the_project(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project_id, _ = await _seed(sessionmaker)
        async with sessionmaker() as session:
            selected = await select_items(session, project_id)
        assert [row.path for row in selected] == ["a.png"]

    async def test_force_includes_items_that_already_have_one(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project_id, _ = await _seed(sessionmaker)
        async with sessionmaker() as session:
            selected = await select_items(session, project_id, force=True)
        assert sorted(row.path for row in selected) == ["a.png", "b.png"]

    async def test_item_ids_narrows_the_selection(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project_id, rows = await _seed(sessionmaker)
        async with sessionmaker() as session:
            selected = await select_items(
                session, project_id, force=True, item_ids=[rows[1].id, rows[3].id]
            )
        # c.png belongs to another project and is never returned.
        assert [row.path for row in selected] == ["b.png"]


class TestRenderPdfThumbnail:
    def test_renders_page_one_and_measures_the_document(self) -> None:
        rendered = render_pdf_thumbnail(pdf(["Invoice", "", ""], rotate={2: 270}), 64)
        with Image.open(io.BytesIO(rendered.jpeg)) as image:
            assert image.format == "JPEG"
            assert max(image.size) == 64
            assert image.size[0] < image.size[1]  # portrait page 1
        assert rendered.meta == {
            "page_count": 3,
            "page_sizes": [[600.0, 800.0], [600.0, 800.0], [800.0, 600.0]],
            "text_layer": True,
        }

    def test_a_scan_has_no_text_layer(self) -> None:
        assert render_pdf_thumbnail(pdf([""]), 32).meta["text_layer"] is False

    def test_page_sizes_are_capped_but_the_count_is_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.services.thumbnails.MAX_PAGE_SIZES", 2)
        meta = render_pdf_thumbnail(pdf(["a", "b", "c", "d"]), 32).meta
        assert meta["page_count"] == 4
        assert len(meta["page_sizes"]) == 2

    @pytest.mark.parametrize("data", [b"", b"%PDF-1.4\n", b"not a pdf at all"])
    def test_rejects_what_pdfium_cannot_open(self, data: bytes) -> None:
        with pytest.raises(ThumbnailError):
            render_pdf_thumbnail(data, 32)

    def test_rejects_a_non_positive_size(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            render_pdf_thumbnail(pdf(["x"]), 0)
