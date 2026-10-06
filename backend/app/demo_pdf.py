"""Two demo projects of PDF documents (TOOL), part of `app.demo seed`.

The same two generated PDFs — a one-page invoice and a two-page letter, all
names fictional — go into two projects:

- "Demo: PDF documents" (layout mode): field boxes (`amount`, `date`,
  `email`, `total`), entity spans (`PER`, `ORG`, `LOC`) and a `works_for`
  relation, annotated on the rendered page. The reference model pre-labels
  it: its PDF field rules give the boxes, its NER the spans.
- "Demo: PDF text mode" (`settings.pdf_mode: text`): the worker extracts
  each PDF's text once and the text annotator labels it, with the PDF
  beside it.

Both jobs (`prelabel`, `extract_text`) go through the queue, so the worker
must be running; without a queue the seed prints how to start them later.
Idempotent, like the rest of the seed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.registry import build_connector
from app.db.session import get_sessionmaker
from app.models import (
    Connector,
    JobType,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Model,
    ModelTask,
    ModelVersion,
    Project,
    ProjectRole,
)
from app.services.jobs import submit_job
from app.services.queue import JobQueue, QueueUnavailableError, create_queue
from app.services.scanning import scan_source

LAYOUT_PROJECT_NAME = "Demo: PDF documents"
LAYOUT_PREFIX = "pdf-docs/"
TEXT_PROJECT_NAME = "Demo: PDF text mode"
TEXT_PREFIX = "pdf-text/"
PDF_MODEL_NAME = "Reference model, PDF (compose)"

_ENTITIES: list[dict[str, Any]] = [
    {"name": "PER", "display_name": "Person", "color": "#f59e0b", "tools": ["span"]},
    {"name": "ORG", "display_name": "Organisation", "color": "#2563eb", "tools": ["span"]},
    {"name": "LOC", "display_name": "Place", "color": "#16a34a", "tools": ["span"]},
    {"name": "works_for", "display_name": "works for", "color": "#9333ea", "tools": ["relation"]},
]

LAYOUT_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {"name": "total", "display_name": "Total", "color": "#e11d48", "tools": ["bbox"]},
        # Named like the reference model's PDF field rules, so its boxes land here.
        {"name": "amount", "display_name": "Amount", "color": "#db2777", "tools": ["bbox"]},
        {"name": "date", "display_name": "Date", "color": "#0891b2", "tools": ["bbox"]},
        {"name": "email", "display_name": "E-mail", "color": "#64748b", "tools": ["bbox"]},
        *_ENTITIES,
    ],
    "classification": [],
}

TEXT_SCHEMA: dict[str, Any] = {"version": 1, "classes": _ENTITIES, "classification": []}

#: `(x, y, font size, text)` per line, PDF user space (origin bottom-left).
_Line = tuple[int, int, int, str]

DOCUMENTS: dict[str, list[list[_Line]]] = {
    "invoice.pdf": [
        [
            (72, 750, 20, "INVOICE"),
            (72, 720, 11, "Northwind Analytics Oy"),
            (72, 705, 11, "Mannerheimintie 12, 00100 Helsinki"),
            (360, 720, 11, "Invoice no: 2026-104"),
            (360, 705, 11, "Date: 05.10.2026"),
            (360, 690, 11, "Due: 04.11.2026"),
            (72, 660, 11, "Bill to: Contoso Ltd, Tampere"),
            (72, 645, 11, "Attn: Anna Virtanen"),
            (72, 600, 11, "Annotation services, September"),
            (420, 600, 11, "980,00 EUR"),
            (72, 582, 11, "Model review and quality report"),
            (420, 582, 11, "260,00 EUR"),
            (72, 550, 13, "Total"),
            (420, 550, 13, "1240,00 EUR"),
            (72, 500, 10, "Questions: billing@northwind.example"),
        ]
    ],
    "letter.pdf": [
        [
            (72, 750, 11, "Contoso Ltd"),
            (72, 735, 11, "Tampere, 5 October 2026"),
            (72, 700, 11, "Dear Anna Virtanen,"),
            (72, 675, 11, "Thank you for meeting Mikko Laine and me in Helsinki last week."),
            (72, 660, 11, "Northwind Analytics will deliver the labelled dataset by the end of"),
            (72, 645, 11, "November, and Mikko Laine will lead the review for Contoso."),
        ],
        [
            (72, 750, 11, "The pilot covers 2400 scanned contracts from the Contoso archives"),
            (72, 735, 11, "in Turku and Oulu. Invoices go to billing@contoso.example."),
            (72, 700, 11, "Kind regards,"),
            (72, 670, 11, "Liisa Korhonen"),
            (72, 655, 11, "Head of Data, Contoso Ltd"),
        ],
    ],
}


def pdf_bytes(pages: list[list[_Line]]) -> bytes:
    """A minimal A4 PDF with a real text layer (Helvetica), one content stream per page."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    kids = []
    for index, lines in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        stream = "".join(
            f"BT /F1 {size} Tf {x} {y} Td ({escape(text)}) Tj ET\n" for x, y, size, text in lines
        )
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}endstream"
        objects[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        kids.append(f"{page_id} 0 R")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"

    body = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(body)
        body += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    xref = len(body)
    size = max(objects) + 1
    body += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        body += f"{offsets[number]:010d} 00000 n \n".encode()
    body += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


async def _ensure_project(
    session: AsyncSession,
    *,
    org_id: UUID,
    owner_id: UUID | None,
    connector: Connector,
    name: str,
    description: str,
    prefix: str,
    schema: dict[str, Any],
    settings: dict[str, Any],
) -> Project:
    """The project (created when missing) with its schema and owner membership."""
    project = await session.scalar(
        select(Project).where(Project.organization_id == org_id, Project.name == name)
    )
    if project is None:
        project = Project(
            organization_id=org_id,
            name=name,
            description=description,
            source_connector_id=connector.id,
            result_connector_id=connector.id,
            source_prefix=prefix,
            source_glob="*.pdf",
            workflow={},
            settings=settings,
        )
        session.add(project)
        await session.flush()
        print(f"Created project {name!r}")
    if project.label_schema_id is None:
        label_schema = LabelSchema(project_id=project.id, name=name.removeprefix("Demo: "))
        session.add(label_schema)
        await session.flush()
        session.add(
            LabelSchemaVersion(label_schema_id=label_schema.id, version=1, definition=schema)
        )
        project.label_schema_id = label_schema.id
    if owner_id is not None and not await session.scalar(
        select(Membership).where(
            Membership.project_id == project.id, Membership.user_id == owner_id
        )
    ):
        session.add(Membership(user_id=owner_id, project_id=project.id, role=ProjectRole.OWNER))
    return project


async def _pdf_model_version(session: AsyncSession, org_id: UUID) -> ModelVersion:
    """The compose reference model registered for the PDF demo, with an empty
    (identity) class mapping: the project schema goes to the model as is, so
    its `amount` / `date` / `email` boxes and `PER` / `ORG` / `LOC` spans land
    in the same-named classes (BYOM-2). The image demo's version maps only
    `object`, which would send it no PDF class at all.
    """
    from app.demo import DEMO_MODEL_URL

    model = await session.scalar(
        select(Model).where(
            Model.organization_id == org_id,
            Model.name == PDF_MODEL_NAME,
            Model.deleted_at.is_(None),
        )
    )
    if model is None:
        model = Model(
            organization_id=org_id,
            name=PDF_MODEL_NAME,
            task=ModelTask.DETECT,
            endpoint_url=DEMO_MODEL_URL,
            identity_type="none",
            secret_ref=None,
        )
        session.add(model)
        await session.flush()
        print(f"Registered model {PDF_MODEL_NAME!r} v1")
    version: ModelVersion | None = await session.scalar(
        select(ModelVersion)
        .where(ModelVersion.model_id == model.id)
        .order_by(ModelVersion.version.desc())
        .limit(1)
    )
    if version is None:
        version = ModelVersion(model_id=model.id, version=1, class_mapping={}, metrics={})
        session.add(version)
        await session.flush()
    return version


async def seed_pdf_projects(
    org_id: UUID,
    owner_id: UUID | None,
    connector: Connector,
    secret: str,
    queue: JobQueue | None = None,
) -> None:
    """Upload the PDFs, create both projects, scan them and queue their jobs.

    `queue` is for tests; by default the seed connects to the app's Redis.
    """
    storage = build_connector(connector.type.value, dict(connector.config), secret)
    try:
        for name, pages in DOCUMENTS.items():
            data = pdf_bytes(pages)
            for prefix in (LAYOUT_PREFIX, TEXT_PREFIX):
                await storage.write(f"{prefix}{name}", data, "application/pdf")

        async with get_sessionmaker()() as session:
            layout = await _ensure_project(
                session,
                org_id=org_id,
                owner_id=owner_id,
                connector=connector,
                name=LAYOUT_PROJECT_NAME,
                description="Seeded by `python -m app.demo seed`: field boxes, entity spans "
                "and relations on the rendered PDF page. Safe to delete.",
                prefix=LAYOUT_PREFIX,
                schema=LAYOUT_SCHEMA,
                settings={},
            )
            text = await _ensure_project(
                session,
                org_id=org_id,
                owner_id=owner_id,
                connector=connector,
                name=TEXT_PROJECT_NAME,
                description="Seeded by `python -m app.demo seed`: each PDF's text is extracted "
                "once and labelled as text, with the PDF beside it. Safe to delete.",
                prefix=TEXT_PREFIX,
                schema=TEXT_SCHEMA,
                settings={"pdf_mode": "text"},
            )
            for project, prefix in ((layout, LAYOUT_PREFIX), (text, TEXT_PREFIX)):
                result = await scan_source(
                    session,
                    project_id=project.id,
                    connector=connector,
                    storage=storage,
                    prefix=prefix,
                )
                print(f"{project.name}: {result.created} item(s) created, {result.updated} updated")
            version = await _pdf_model_version(session, org_id)
            await session.commit()

            owned_queue = queue is None
            if queue is None:
                try:
                    queue = await create_queue()
                except QueueUnavailableError:
                    print(
                        "PDF demos: the job queue is not reachable. Start the stack, then use "
                        f"'Extract PDF text' in {TEXT_PROJECT_NAME!r}'s settings and the "
                        f"Pre-label panel in {LAYOUT_PROJECT_NAME!r}."
                    )
                    return
            try:
                # Pending or failed texts only: a finished extraction selects nothing.
                await submit_job(
                    session, queue, project_id=text.id, job_type=JobType.EXTRACT_TEXT, payload={}
                )
                print(f"{TEXT_PROJECT_NAME}: queued text extraction")
                # Idempotent per model version: a re-seed adds no second draft.
                await submit_job(
                    session,
                    queue,
                    project_id=layout.id,
                    job_type=JobType.PRELABEL,
                    payload={"model_version_id": str(version.id)},
                )
                print(f"{LAYOUT_PROJECT_NAME}: queued pre-labelling by the reference model")
            finally:
                if owned_queue:
                    await queue.aclose()
    finally:
        await storage.aclose()
