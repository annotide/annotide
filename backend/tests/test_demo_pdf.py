"""The PDF demo projects of `app.demo seed` (TOOL): layout mode and text mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import demo_pdf
from app.exporters.base import pdf_document
from app.models import (
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    Job,
    JobType,
    MediaType,
    Model,
    ModelVersion,
    Project,
)
from app.services.pdf_text import extract_pdf_words
from app.worker import jobs
from tests import test_worker as tw
from tests.support import FakeJobQueue
from tests.test_worker import ORG_ID, _run

# Fixtures, shared with the worker tests.
engine = tw.engine
sessionmaker = tw.sessionmaker
ctx = tw.ctx


def test_the_sample_pdfs_have_a_text_layer_on_every_page() -> None:
    invoice, _ = pdf_document(
        extract_pdf_words(demo_pdf.pdf_bytes(demo_pdf.DOCUMENTS["invoice.pdf"]))
    )
    letter_words = extract_pdf_words(demo_pdf.pdf_bytes(demo_pdf.DOCUMENTS["letter.pdf"]))
    assert "Attn: Anna Virtanen\n" in invoice
    assert "Total 1240,00 EUR" in invoice
    assert {word.page for word in letter_words} == {1, 2}


def _seed_connector(sessionmaker: async_sessionmaker[AsyncSession], root: Path) -> Connector:
    async def _create() -> Connector:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=ORG_ID,
                name="demo",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                secret_ref=None,
                config={"root": str(root)},
            )
            session.add(connector)
            await session.commit()
            return connector

    return _run(_create())


def test_seed_creates_both_projects_once_and_queues_their_jobs(
    ctx: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo_pdf, "get_sessionmaker", lambda: sessionmaker)
    connector = _seed_connector(sessionmaker, tmp_path)
    queue = FakeJobQueue()

    _run(demo_pdf.seed_pdf_projects(ORG_ID, None, connector, "", queue=queue))
    _run(demo_pdf.seed_pdf_projects(ORG_ID, None, connector, "", queue=queue))

    async def _load() -> tuple[dict[str, Project], list[Item], list[Job]]:
        async with sessionmaker() as session:
            projects = {p.name: p for p in await session.scalars(select(Project))}
            return (
                projects,
                list(await session.scalars(select(Item))),
                list(await session.scalars(select(Job))),
            )

    projects, items, queued = _run(_load())
    layout = projects[demo_pdf.LAYOUT_PROJECT_NAME]
    text = projects[demo_pdf.TEXT_PROJECT_NAME]
    assert text.settings == {"pdf_mode": "text"}
    by_project = {
        project_id: sorted((i.path, i.media_type) for i in items if i.project_id == project_id)
        for project_id in (layout.id, text.id)
    }
    # Re-seeding is idempotent: two PDFs per project, no duplicates.
    assert by_project[layout.id] == [
        ("pdf-docs/invoice.pdf", MediaType.PDF),
        ("pdf-docs/letter.pdf", MediaType.PDF),
    ]
    assert by_project[text.id] == [
        ("pdf-text/invoice.pdf", MediaType.TEXT),
        ("pdf-text/letter.pdf", MediaType.TEXT),
    ]
    assert {(job.type, job.project_id) for job in queued} == {
        (JobType.EXTRACT_TEXT, text.id),
        (JobType.PRELABEL, layout.id),
    }

    # Pre-labelled by a version with an empty (identity) mapping, registered once.
    async def _versions() -> list[ModelVersion]:
        async with sessionmaker() as session:
            return list(
                await session.scalars(
                    select(ModelVersion)
                    .join(Model, Model.id == ModelVersion.model_id)
                    .where(Model.name == demo_pdf.PDF_MODEL_NAME)
                )
            )

    [version] = _run(_versions())
    assert version.class_mapping == {}
    prelabels = [job for job in queued if job.type is JobType.PRELABEL]
    assert {job.payload["model_version_id"] for job in prelabels} == {str(version.id)}

    # The queued extraction makes the text-mode items ready to annotate.
    extract = next(job for job in queued if job.type is JobType.EXTRACT_TEXT)
    result = _run(jobs.extract_text(ctx, str(extract.id)))
    assert result["extracted"] == 2
    texts = (tmp_path / "text").glob("*.txt")
    assert any("Liisa Korhonen" in path.read_text() for path in texts)


def test_seed_without_a_queue_still_creates_the_projects(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(demo_pdf, "get_sessionmaker", lambda: sessionmaker)

    async def no_queue() -> FakeJobQueue:
        raise demo_pdf.QueueUnavailableError("redis down")

    monkeypatch.setattr(demo_pdf, "create_queue", no_queue)
    connector = _seed_connector(sessionmaker, tmp_path)

    _run(demo_pdf.seed_pdf_projects(ORG_ID, None, connector, ""))

    async def _names() -> set[str]:
        async with sessionmaker() as session:
            return set(await session.scalars(select(Project.name)))

    assert _run(_names()) == {demo_pdf.LAYOUT_PROJECT_NAME, demo_pdf.TEXT_PROJECT_NAME}
    assert "Extract PDF text" in capsys.readouterr().out
