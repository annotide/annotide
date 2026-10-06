"""PDF text mode: the scan, and the `extract_text` job (CONTRACTS.md *PDF text mode*)."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exporters.base import pdf_document
from app.models import Item, JobStatus, JobType, MediaType, Model, ModelTask, Project, Task
from app.services.models import ModelUnavailable, OcrPage
from app.services.pdf_text import extract_pdf_words, group_ocr_lines
from app.services.text_sources import text_source
from app.worker import jobs
from tests import test_worker as tw
from tests.support import FakeJobQueue
from tests.test_pdf_export import make_pdf
from tests.test_worker import (
    ORG_ID,
    Fixture,
    _load_item,
    _load_job,
    _run,
    _seed_item_with_versions,
    _seed_job,
    _seed_project,
)

# Fixtures, shared with the worker tests.
engine = tw.engine
sessionmaker = tw.sessionmaker
ctx = tw.ctx
fake_client = tw.fake_client

PAGES = [
    [(100, 700, "Anna Smith lives"), (100, 680, "in Paris today")],
    [(100, 700, "Second page")],
]


def _text_project(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> Fixture:
    fx = _seed_project(sessionmaker, tmp_path)

    async def _set() -> None:
        async with sessionmaker() as session:
            project = await session.get(Project, fx.project.id)
            assert project is not None
            project.settings = {"pdf_mode": "text"}
            await session.commit()

    _run(_set())
    (tmp_path / "images").mkdir()
    return fx


def _scan(ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture) -> Any:
    job = _seed_job(sessionmaker, fx.project.id, JobType.SCAN_SOURCE)
    return _run(jobs.scan_source(ctx, str(job.id)))


def _extract(
    ctx: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    fx: Fixture,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = _seed_job(sessionmaker, fx.project.id, JobType.EXTRACT_TEXT, payload)
    result = _run(jobs.extract_text(ctx, str(job.id)))
    assert _load_job(sessionmaker, job.id).status is JobStatus.SUCCEEDED
    return result


def _tasks(sessionmaker: async_sessionmaker[AsyncSession], item_id: uuid.UUID) -> list[Task]:
    async def _load() -> list[Task]:
        async with sessionmaker() as session:
            return list(await session.scalars(select(Task).where(Task.item_id == item_id)))

    return _run(_load())


def _seed_ocr_model(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async def _create() -> None:
        async with sessionmaker() as session:
            session.add(
                Model(
                    organization_id=ORG_ID,
                    name="ocr",
                    task=ModelTask.OCR,
                    endpoint_url="http://ocr.example",
                    identity_type="none",
                )
            )
            await session.commit()

    _run(_create())


class FakeOcr:
    pages: ClassVar[dict[int, list[Any]]] = {}
    fail: ClassVar[bool] = False
    asked: ClassVar[list[int]] = []

    @classmethod
    async def for_model(cls, *args: Any, **kwargs: Any) -> FakeOcr:
        return cls()

    async def __aenter__(self) -> FakeOcr:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def ocr(self, item: Any, page: int) -> OcrPage:
        type(self).asked.append(page)
        if type(self).fail:
            raise ModelUnavailable("down")
        return OcrPage(
            page=page, width=600, height=800, engine="fake", words=type(self).pages[page]
        )


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> type[FakeOcr]:
    cls = type("FakeOcr", (FakeOcr,), {"pages": {}, "fail": False, "asked": []})
    monkeypatch.setattr(jobs, "ModelClient", cls)
    return cls


def _item_for(sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture) -> Item:
    async def _load() -> Item:
        async with sessionmaker() as session:
            return (
                await session.scalars(select(Item).where(Item.project_id == fx.project.id))
            ).one()

    return _run(_load())


class TestScan:
    def test_text_mode_creates_a_pending_text_item_without_a_task(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        queue = FakeJobQueue()
        ctx["queue"] = queue

        result = _scan(ctx, sessionmaker, fx)

        item = _item_for(sessionmaker, fx)
        assert item.media_type is MediaType.TEXT
        assert item.path == "images/a.pdf"
        assert item.meta["pdf_text"] == {"status": "pending", "path": f"text/{item.id}.txt"}
        assert item.meta["views"] == [{"path": "images/a.pdf", "label": "PDF"}]
        assert _tasks(sessionmaker, item.id) == []
        assert result["tasks_opened"] == 0 and result["pdf_texts_pending"] == 1
        assert "extract_text" in [kind for _, kind in queue.enqueued]
        assert result["extract_text_job_id"]

    def test_layout_mode_is_unchanged(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        queue = FakeJobQueue()
        ctx["queue"] = queue

        result = _scan(ctx, sessionmaker, fx)

        item = _item_for(sessionmaker, fx)
        assert item.media_type is MediaType.PDF
        assert "pdf_text" not in item.meta
        assert len(_tasks(sessionmaker, item.id)) == 1
        assert "extract_text" not in [kind for _, kind in queue.enqueued]
        assert "extract_text_job_id" not in result


class TestExtract:
    def _scanned(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        pages: list[list[tuple[int, int, str]]],
    ) -> tuple[Fixture, Item]:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(pages))
        _scan(ctx, sessionmaker, fx)
        return fx, _item_for(sessionmaker, fx)

    def test_text_equals_the_layout_document_and_opens_the_task(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx, item = self._scanned(ctx, sessionmaker, tmp_path, PAGES)

        result = _extract(ctx, sessionmaker, fx)

        expected, _ = pdf_document(extract_pdf_words(make_pdf(PAGES)))
        written = (tmp_path / "text" / f"{item.id}.txt").read_bytes().decode()
        assert written == expected
        meta = _load_item(sessionmaker, item.id).meta["pdf_text"]
        assert meta["status"] == "ready"
        assert meta["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        first_end = len("Anna Smith lives\nin Paris today")
        assert meta["pages"] == [[0, first_end], [first_end + 2, len(expected)]]
        assert meta["ocr_pages"] == [] and meta["empty_pages"] == []
        assert len(_tasks(sessionmaker, item.id)) == 1
        assert result["selected"] == result["extracted"] == result["tasks_opened"] == 1
        # A second run has nothing to do.
        assert _extract(ctx, sessionmaker, fx)["selected"] == 0

    def test_a_page_without_a_text_layer_is_read_by_ocr(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_ocr: type[FakeOcr],
    ) -> None:
        _seed_ocr_model(sessionmaker)
        fake_ocr.pages = {
            2: [
                ("Scanned", (10.0, 10.0, 60.0, 22.0)),
                ("words", (65.0, 10.0, 100.0, 22.0)),
                ("next", (10.0, 40.0, 40.0, 52.0)),
            ]
        }
        fx, item = self._scanned(ctx, sessionmaker, tmp_path, [PAGES[0], []])

        result = _extract(ctx, sessionmaker, fx)

        text = (tmp_path / "text" / f"{item.id}.txt").read_text()
        assert text == "Anna Smith lives\nin Paris today\n\nScanned words\nnext"
        meta = _load_item(sessionmaker, item.id).meta["pdf_text"]
        assert meta["ocr_pages"] == [2] and meta["empty_pages"] == []
        assert meta["pages"][1] == [len("Anna Smith lives\nin Paris today") + 2, len(text)]
        assert fake_ocr.asked == [2]
        assert result["ocr_pages"] == 1

    def test_no_ocr_model_leaves_the_page_empty(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx, item = self._scanned(ctx, sessionmaker, tmp_path, [PAGES[0], []])

        result = _extract(ctx, sessionmaker, fx)

        meta = _load_item(sessionmaker, item.id).meta["pdf_text"]
        assert meta["empty_pages"] == [2] and meta["ocr_pages"] == []
        end = len("Anna Smith lives\nin Paris today")
        assert meta["pages"][1] == [end, end]
        assert result["empty_pages"] == 1

    def test_a_failing_ocr_model_is_reported_and_the_page_is_empty(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_ocr: type[FakeOcr],
    ) -> None:
        _seed_ocr_model(sessionmaker)
        fake_ocr.fail = True
        fx, item = self._scanned(ctx, sessionmaker, tmp_path, [PAGES[0], []])

        result = _extract(ctx, sessionmaker, fx)

        assert _load_item(sessionmaker, item.id).meta["pdf_text"]["empty_pages"] == [2]
        assert result["extracted"] == 1 and "OCR failed" in result["errors"][0]

    def test_a_corrupt_pdf_fails_without_a_task(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(b"%PDF-1.4 not really")
        _scan(ctx, sessionmaker, fx)
        item = _item_for(sessionmaker, fx)

        result = _extract(ctx, sessionmaker, fx)

        meta = _load_item(sessionmaker, item.id).meta["pdf_text"]
        assert meta["status"] == "failed" and meta["error"]
        assert result["failed"] == 1 and _tasks(sessionmaker, item.id) == []

    def test_force_re_extracts_but_skips_annotated_items(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx, _ = self._scanned(ctx, sessionmaker, tmp_path, PAGES)
        _extract(ctx, sessionmaker, fx)
        assert _extract(ctx, sessionmaker, fx, {"force": True})["extracted"] == 1

        other = _seed_item_with_versions(sessionmaker, fx, "images/b.pdf")

        async def _make_pdf_text() -> None:
            async with sessionmaker() as session:
                row = await session.get(Item, other.id)
                assert row is not None
                row.media_type = MediaType.TEXT
                row.meta = {"pdf_text": {"status": "ready", "path": "text/x.txt"}}
                await session.commit()

        _run(_make_pdf_text())
        result = _extract(ctx, sessionmaker, fx, {"force": True})
        assert result["skipped_annotated"] == 1 and result["selected"] == 1

    def test_rescan_with_a_new_etag_keeps_the_text(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx, item = self._scanned(ctx, sessionmaker, tmp_path, PAGES)
        _extract(ctx, sessionmaker, fx)
        before = _load_item(sessionmaker, item.id).meta["pdf_text"]
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf([[(100, 700, "Changed")]]))

        result = _scan(ctx, sessionmaker, fx)

        after = _load_item(sessionmaker, item.id)
        assert result["items_updated"] == 1 and result["items_created"] == 0
        assert after.meta["pdf_text"] == before
        assert _extract(ctx, sessionmaker, fx)["selected"] == 0


def test_ocr_lines_follow_the_span_line_rule() -> None:
    words = group_ocr_lines(
        3,
        [
            ("a", (0.0, 10.0, 10.0, 20.0)),
            ("b", (12.0, 12.0, 22.0, 22.0)),
            ("c", (0.0, 40.0, 10.0, 50.0)),
        ],
    )
    assert [w.line_break_before for w in words] == [False, False, True]
    assert {w.page for w in words} == {3}


class TestReaders:
    """Pre-labelling and the text exports read the extracted text, not the PDF."""

    def test_prelabel_sends_the_extracted_text_once_it_is_ready(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_client: type[tw.FakeModelClient],
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        _scan(ctx, sessionmaker, fx)
        item = _item_for(sessionmaker, fx)
        version = tw._seed_model_version(sessionmaker)
        fake_client.media_types = ["text"]

        def prelabel() -> dict[str, Any]:
            job = _seed_job(
                sessionmaker, fx.project.id, JobType.PRELABEL, {"model_version_id": str(version.id)}
            )
            return _run(jobs.prelabel(ctx, str(job.id)))

        # Pending: nothing to send yet.
        assert prelabel()["skipped_media"] == 1
        assert fake_client.calls == []

        _extract(ctx, sessionmaker, fx)
        prelabel()
        [(sent, _, _)] = fake_client.calls
        assert [(p.media_type, p.id) for p in sent] == [("text", str(item.id))]
        assert f"text/{item.id}.txt" in sent[0].url
        assert "a.pdf" not in sent[0].url

    def test_text_exports_read_the_extracted_text(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        _scan(ctx, sessionmaker, fx)

        async def read() -> dict[uuid.UUID, str]:
            async with sessionmaker() as session:
                project = await session.get(Project, fx.project.id)
                assert project is not None
                items = list(await session.scalars(select(Item)))
                entries = [SimpleNamespace(item=item) for item in items]
                return await jobs._read_text_sources(session, project, entries)  # type: ignore[arg-type]  # duck-typed DatasetEntry

        assert _run(read()) == {}  # pending: skipped with the usual warning
        _extract(ctx, sessionmaker, fx)
        item = _item_for(sessionmaker, fx)
        expected, _ = pdf_document(extract_pdf_words(make_pdf(PAGES)))
        assert _run(read()) == {item.id: expected}


class TestRetriesAndLimits:
    def test_every_scan_requeues_texts_that_are_not_ready(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ctx["queue"] = FakeJobQueue()
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(b"%PDF-1.4 broken")
        first = _scan(ctx, sessionmaker, fx)
        assert first["extract_text_job_id"]
        _extract(ctx, sessionmaker, fx)
        assert _item_for(sessionmaker, fx).meta["pdf_text"]["status"] == "failed"

        # A rescan that creates nothing still retries the failed text ...
        again = _scan(ctx, sessionmaker, fx)
        assert again.get("items_created", 0) == 0
        assert again["extract_text_job_id"]

        # ... and once it is ready, scans stop queueing extraction.
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        _extract(ctx, sessionmaker, fx)
        assert _item_for(sessionmaker, fx).meta["pdf_text"]["status"] == "ready"
        assert "extract_text_job_id" not in _scan(ctx, sessionmaker, fx)

    def test_a_layout_project_queues_no_extraction(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ctx["queue"] = FakeJobQueue()
        fx = _seed_project(sessionmaker, tmp_path)
        (tmp_path / "images").mkdir()
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        assert "extract_text_job_id" not in _scan(ctx, sessionmaker, fx)

    def test_ocr_stops_after_failures_in_a_row(
        self,
        ctx: dict[str, Any],
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        fake_ocr: type[FakeOcr],
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        _seed_ocr_model(sessionmaker)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf([[] for _ in range(6)]))
        _scan(ctx, sessionmaker, fx)
        fake_ocr.fail = True

        result = _extract(ctx, sessionmaker, fx)

        assert len(fake_ocr.asked) == jobs._OCR_MAX_FAILURES_IN_A_ROW
        meta = _item_for(sessionmaker, fx).meta["pdf_text"]
        assert meta["status"] == "ready"
        assert meta["empty_pages"] == [1, 2, 3, 4, 5, 6]
        assert result["empty_pages"] == 6

    def test_the_text_stays_on_the_connector_it_was_written_to(
        self, ctx: dict[str, Any], sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _text_project(sessionmaker, tmp_path)
        (tmp_path / "images" / "a.pdf").write_bytes(make_pdf(PAGES))
        _scan(ctx, sessionmaker, fx)
        _extract(ctx, sessionmaker, fx)
        item = _item_for(sessionmaker, fx)
        meta = item.meta["pdf_text"]

        project = Project(result_connector_id=uuid.uuid4())
        source = text_source(item, project)
        assert source is not None
        assert str(source.connector_id) == meta["connector_id"] != str(project.result_connector_id)
        assert source.path == f"text/{item.id}.txt"
