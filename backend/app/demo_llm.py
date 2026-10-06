"""A demo project of LLM evaluation items (§5 LLM-data), part of `app.demo seed`.

Three `.llm.json` documents in the demo container: a pairwise preference, a
three-way ranking, and a support conversation to rate turn by turn. The
schema has one criterion of each kind: a `preference` ranking with a
rationale, a 1-5 `helpfulness` rating, and a `safe` classification.
Idempotent, like the rest of the seed.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.connectors.registry import build_connector
from app.db.session import get_sessionmaker
from app.models import (
    Connector,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Project,
    ProjectRole,
)
from app.services.scanning import scan_source

LLM_PROJECT_NAME = "Demo: LLM evaluation"
LLM_PREFIX = "llm-eval/"

LLM_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "preference",
            "display_name": "Preference",
            "color": "#2563eb",
            "tools": ["ranking"],
            "attributes": [{"name": "why", "type": "text", "required": False}],
        },
        {
            "name": "helpfulness",
            "display_name": "Helpfulness",
            "color": "#16a34a",
            "tools": ["rating"],
            "attributes": [],
            "scale": {"min": 1, "max": 5, "labels": {"1": "Useless", "5": "Excellent"}},
        },
    ],
    "classification": [{"name": "safe", "type": "boolean", "required": False}],
}

LLM_DOCUMENTS: dict[str, dict[str, Any]] = {
    "01-preference.llm.json": {
        "messages": [{"role": "user", "content": "What is the capital of Finland?"}],
        "responses": [
            {"id": "a", "content": "Helsinki.", "model": "model-x"},
            {"id": "b", "content": "The capital of Finland is Turku.", "model": "model-y"},
        ],
    },
    "02-ranking.llm.json": {
        "messages": [
            {"role": "system", "content": "Answer in one short paragraph."},
            {"role": "user", "content": "Why do annotation tools version their label schemas?"},
        ],
        "responses": [
            {
                "id": "a",
                "content": "So that every annotation still means what it meant when it was "
                "made: a new schema version can rename or split classes without "
                "silently changing old labels.",
            },
            {"id": "b", "content": "Versions are faster."},
            {
                "id": "c",
                "content": "Because schemas change; a version lets old annotations be mapped "
                "to the new classes instead of being lost.",
            },
        ],
    },
    "03-conversation.llm.json": {
        "messages": [
            {"role": "user", "content": "I forgot my password."},
            {
                "role": "assistant",
                "content": "Use 'Forgot password' on the sign-in page; a reset link "
                "arrives by e-mail within a few minutes.",
            },
            {"role": "user", "content": "No e-mail came."},
            {"role": "assistant", "content": "Please try again later."},
        ],
        "responses": [],
    },
}


async def seed_llm_project(
    org_id: UUID, owner_id: UUID | None, connector: Connector, secret: str
) -> None:
    """Upload the documents, create the project and scan it."""
    storage = build_connector(connector.type.value, dict(connector.config), secret)
    try:
        for name, document in LLM_DOCUMENTS.items():
            await storage.write(
                f"{LLM_PREFIX}{name}",
                json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )

        async with get_sessionmaker()() as session:
            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == org_id, Project.name == LLM_PROJECT_NAME
                )
            )
            if project is None:
                project = Project(
                    organization_id=org_id,
                    name=LLM_PROJECT_NAME,
                    description="Seeded by `python -m app.demo seed`: rank, rate and review "
                    "LLM responses. Safe to delete.",
                    source_connector_id=connector.id,
                    result_connector_id=connector.id,
                    source_prefix=LLM_PREFIX,
                    workflow={},
                    settings={},
                )
                session.add(project)
                await session.flush()
                print(f"Created project {LLM_PROJECT_NAME!r}")
            if project.label_schema_id is None:
                schema = LabelSchema(project_id=project.id, name="LLM evaluation")
                session.add(schema)
                await session.flush()
                session.add(
                    LabelSchemaVersion(label_schema_id=schema.id, version=1, definition=LLM_SCHEMA)
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
                prefix=LLM_PREFIX,
            )
            await session.commit()
        print(f"LLM evaluation: {result.created} item(s) created, {result.updated} updated")
    finally:
        await storage.aclose()
