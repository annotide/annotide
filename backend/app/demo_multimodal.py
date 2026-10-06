"""A demo project of image + caption tasks (§5 multimodal), part of `app.demo seed`.

Three of the bundled street photos, each with a `.txt` caption beside it,
in a project with `settings.companion_extensions = [".txt"]`: the scan turns
every caption into a companion view of its photo, and the task is to judge
whether the caption matches (one is deliberately wrong). Idempotent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.connectors.registry import build_connector
from app.db.session import get_sessionmaker
from app.demo import sample_images
from app.models import (
    Connector,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Project,
    ProjectRole,
)
from app.services.scanning import scan_source

MULTIMODAL_PROJECT_NAME = "Demo: image and caption"
MULTIMODAL_PREFIX = "image-caption/"

MULTIMODAL_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "car",
            "display_name": "Car",
            "color": "#e11d48",
            "tools": ["bbox"],
            "attributes": [],
        }
    ],
    "classification": [
        {"name": "caption_matches", "type": "boolean", "required": True},
        {
            "name": "problem",
            "type": "select",
            "required": False,
            "options": ["wrong objects", "wrong place", "too vague"],
        },
    ],
}

CAPTIONS = [
    "A city street with cars and pedestrians.",
    "Traffic and road signs on a busy road.",
    "A quiet beach at sunset with no people.",  # wrong on purpose
]


async def seed_multimodal_project(
    org_id: UUID, owner_id: UUID | None, connector: Connector, secret: str
) -> None:
    """Upload photos and captions, create the project and scan it."""
    storage = build_connector(connector.type.value, dict(connector.config), secret)
    try:
        for index, (image, caption) in enumerate(zip(sample_images(), CAPTIONS, strict=False)):
            stem = f"{MULTIMODAL_PREFIX}{index + 1:02d}"
            await storage.write(f"{stem}.jpg", image.read_bytes(), "image/jpeg")
            await storage.write(f"{stem}.txt", caption.encode("utf-8"), "text/plain")

        async with get_sessionmaker()() as session:
            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == org_id, Project.name == MULTIMODAL_PROJECT_NAME
                )
            )
            if project is None:
                project = Project(
                    organization_id=org_id,
                    name=MULTIMODAL_PROJECT_NAME,
                    description="Seeded by `python -m app.demo seed`: judge whether each "
                    "caption matches its photo. Safe to delete.",
                    source_connector_id=connector.id,
                    result_connector_id=connector.id,
                    source_prefix=MULTIMODAL_PREFIX,
                    workflow={},
                    settings={"companion_extensions": [".txt"]},
                )
                session.add(project)
                await session.flush()
                print(f"Created project {MULTIMODAL_PROJECT_NAME!r}")
            if project.label_schema_id is None:
                schema = LabelSchema(project_id=project.id, name="Image and caption")
                session.add(schema)
                await session.flush()
                session.add(
                    LabelSchemaVersion(
                        label_schema_id=schema.id, version=1, definition=MULTIMODAL_SCHEMA
                    )
                )
                project.label_schema_id = schema.id
            if owner_id is not None and not await session.scalar(
                select(Membership).where(
                    Membership.project_id == project.id, Membership.user_id == owner_id
                )
            ):
                session.add(
                    Membership(user_id=owner_id, project_id=project.id, role=ProjectRole.OWNER)
                )
            result = await scan_source(
                session,
                project_id=project.id,
                connector=connector,
                storage=storage,
                prefix=MULTIMODAL_PREFIX,
            )
            await session.commit()
        print(
            f"Image and caption: {result.created} item(s) created, "
            f"{result.companions} caption(s) attached"
        )
    finally:
        await storage.aclose()
