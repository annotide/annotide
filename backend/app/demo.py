"""Seed a working demo: sample media in storage, a project wired to it.

Run once against a fresh stack and the whole loop becomes explorable — storage
with real objects, a connector, a label schema, a project, and items produced
by a genuine scan of that storage rather than inserted behind its back.

    python -m app.demo seed

Everything it creates is idempotent: run it twice and you get the same single
project, not a second copy.

The sample images are real street photographs shipped in `app/demo_samples/`
(CC0, from Wikimedia Commons — see `SOURCES.md` there for authors and links).
They contain cars, road signs and pedestrians, so the demo schema has something
genuine to annotate and a detector something real to find.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_engine, get_sessionmaker
from app.models import (
    Annotation,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Model,
    ModelTask,
    ModelVersion,
    Organization,
    Project,
    ProjectRole,
    User,
)
from app.services.licensing.fingerprint import email_domain
from app.services.scanning import scan_source

# Azurite's well-known development credentials. These are published in
# Microsoft's own documentation and grant access to nothing but a local
# emulator; they are not a secret and must never appear in a real deployment.
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
AZURITE_INTERNAL_URL = f"http://azurite:10000/{AZURITE_ACCOUNT}"
AZURITE_PUBLIC_URL = f"http://localhost:10000/{AZURITE_ACCOUNT}"

#: The Playwright dev server (frontend/playwright.config.ts, `E2E_PORT`). It
#: loads media from Azurite with `crossOrigin`, so it needs its own CORS entry.
E2E_DEV_ORIGIN = "http://localhost:5174"

DEMO_CONTAINER = "media"
DEMO_PREFIX = "samples/"
DEMO_ORG_SLUG = "default"
DEMO_PROJECT_NAME = "Demo: traffic objects"

#: Matches the Label schema JSON in docs/CONTRACTS.md.
DEMO_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "car",
            "display_name": "Car",
            "color": "#e11d48",
            "hotkey": "1",
            "tools": ["bbox", "polygon"],
            "attributes": [
                {"name": "occluded", "type": "boolean", "required": False, "default": False}
            ],
        },
        {
            "name": "sign",
            "display_name": "Sign",
            "color": "#0ea5e9",
            "hotkey": "2",
            "tools": ["bbox"],
            "attributes": [],
        },
        {
            "name": "pedestrian",
            "display_name": "Pedestrian",
            "color": "#f59e0b",
            "hotkey": "3",
            "tools": ["bbox", "polygon", "point"],
            "attributes": [],
        },
    ],
    "classification": [
        {
            "name": "weather",
            "type": "select",
            "required": False,
            "options": ["clear", "rain", "snow"],
        }
    ],
}

#: The reference model service from docker-compose (`model-service/`). It
#: emits a single class, `object`; the class mapping (BYOM-2) turns that into
#: the project's `car` so the schema needs no synthetic model class.
DEMO_MODEL_NAME = "Reference model (compose)"
DEMO_MODEL_URL = "http://model:9000"
#: The same service registered as a `segment` model, which is what the
#: annotator's smart-polygon tool (ML-7) is offered for. Its heuristic
#: `/interactive` answers a polygon for any click or box, so the tool can be
#: tried (and E2E-tested) with no weights.
DEMO_SEGMENT_MODEL_NAME = "Reference segmenter (compose)"
DEMO_CLASS_MAPPING: dict[str, str | None] = {"object": "car"}
#: A distillation + quantization family for the Models page's family view
#: (EXP-8). External producers (no endpoint), so prelabelling never offers
#: them; their metrics are illustrative, not from a run.
DEMO_TEACHER_MODEL_NAME = "Demo: large detector (teacher)"
DEMO_STUDENT_MODEL_NAME = "Demo: small detector (student)"
_DEMO_FAMILY_NOTE = "Illustrative demo metrics (make seed), not a real training run."


def _demo_metrics(
    f1: float, per_class: dict[str, float], size_bytes: int, latency_ms: int, dtype: str
) -> dict[str, Any]:
    """The well-known metric keys (docs/CONTRACTS.md) the family view reads."""
    return {
        "split": "test",
        "f1": f1,
        "precision": round(min(1.0, f1 + 0.03), 2),
        "recall": round(f1 - 0.03, 2),
        "per_class": {
            name: {"f1": value, "precision": value, "recall": value}
            for name, value in per_class.items()
        },
        "size_bytes": size_bytes,
        "latency_ms_p50": latency_ms,
        "latency_device": "demo",
        "dtype": dtype,
    }


# --------------------------------------------------------------------------- #
# Sample images
# --------------------------------------------------------------------------- #

SAMPLES_DIR = Path(__file__).parent / "demo_samples"


def sample_images() -> list[Path]:
    """Every bundled sample photo, in a stable order.

    Sorted by name so ``scene-001-…`` is always first: re-running the seed
    with a smaller ``--count`` uploads a prefix of the same set, not a
    different subset, which keeps the seed idempotent.
    """
    return sorted(SAMPLES_DIR.glob("*.jpg"))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def demo_cors_origins() -> list[str]:
    """The origins the demo storage accepts browser requests from (SRC-7).

    The app's own origins (``APP_CORS_ORIGINS`` and ``APP_FRONTEND_URL``) plus
    the E2E dev server, never ``*``: the demo should show the rule a real
    deployment needs, not one that would pass a security review by accident.
    """
    settings = get_settings()
    origins = [*settings.cors_origins, settings.frontend_url, E2E_DEV_ORIGIN]
    return list(dict.fromkeys(origin.rstrip("/") for origin in origins if origin))


async def _prepare_storage(
    count: int, *, keep: set[str], quiet: bool = False
) -> tuple[int, list[str]]:
    """Create the container, set CORS, upload the sample images, drop stale ones.

    The seeder owns everything under ``DEMO_PREFIX``. Objects there that are
    not in the bundled set (``keep``) are left over from an earlier version of
    this seeder and are deleted, unless ``keep`` names them — the caller adds
    the paths of items that already carry annotations, which must survive.
    Returns the upload count and the deleted object paths.
    """
    from azure.storage.blob import CorsRule
    from azure.storage.blob.aio import BlobServiceClient

    connection = (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={AZURITE_ACCOUNT};AccountKey={AZURITE_KEY};"
        f"BlobEndpoint={AZURITE_INTERNAL_URL};"
    )

    async with BlobServiceClient.from_connection_string(connection) as client:
        # CORS matters more than it looks (SRC-7). The browser fetches media
        # straight from storage on a different origin than the app; without a
        # rule the request is blocked and the annotator shows an empty canvas
        # with nothing useful in the server logs. PUT is for the browser
        # upload path (§12), which writes straight to the container.
        await client.set_service_properties(
            cors=[
                CorsRule(
                    allowed_origins=demo_cors_origins(),
                    allowed_methods=["GET", "HEAD", "PUT", "OPTIONS"],
                    allowed_headers=["*"],
                    exposed_headers=["*"],
                    max_age_in_seconds=3600,
                )
            ]
        )

        container = client.get_container_client(DEMO_CONTAINER)
        # Already existing is the normal case on a re-run.
        with contextlib.suppress(Exception):
            await container.create_container()

        uploaded = 0
        for path in sample_images()[:count]:
            await container.upload_blob(
                name=f"{DEMO_PREFIX}{path.name}",
                data=path.read_bytes(),
                overwrite=True,
                content_type="image/jpeg",
            )
            uploaded += 1

        removed: list[str] = []
        async for blob in container.list_blobs(name_starts_with=DEMO_PREFIX):
            if blob.name in keep:
                continue
            await container.delete_blob(blob.name)
            removed.append(blob.name)

        if not quiet:
            print(f"Uploaded {uploaded} sample image(s) to {DEMO_CONTAINER}/{DEMO_PREFIX}")
            if removed:
                print(f"Removed {len(removed)} stale object(s) under {DEMO_PREFIX}")
        return uploaded, removed


# --------------------------------------------------------------------------- #
# Database wiring
# --------------------------------------------------------------------------- #


async def _seed_model_family(session: AsyncSession, organization_id: UUID) -> None:
    """Teacher → distilled student → int8 copy, once (EXP-8)."""
    seeded = await session.scalar(
        select(Model.id).where(
            Model.organization_id == organization_id,
            Model.name == DEMO_TEACHER_MODEL_NAME,
            Model.deleted_at.is_(None),
        )
    )
    if seeded is not None:
        return
    teacher_model, student_model = (
        Model(
            organization_id=organization_id,
            name=name,
            task=ModelTask.DETECT,
            endpoint_url=None,
            identity_type="none",
            secret_ref=None,
        )
        for name in (DEMO_TEACHER_MODEL_NAME, DEMO_STUDENT_MODEL_NAME)
    )
    session.add_all([teacher_model, student_model])
    await session.flush()
    run = {"note": _DEMO_FAMILY_NOTE}
    teacher = ModelVersion(
        model_id=teacher_model.id,
        version=1,
        class_mapping={},
        metrics=_demo_metrics(
            0.91, {"car": 0.95, "pedestrian": 0.88, "sign": 0.86}, 167_000_000, 410, "fp32"
        ),
        training_run=run,
        derivation="trained",
    )
    session.add(teacher)
    await session.flush()
    student = ModelVersion(
        model_id=student_model.id,
        version=1,
        class_mapping={},
        metrics=_demo_metrics(
            0.87, {"car": 0.94, "pedestrian": 0.85, "sign": 0.74}, 76_000_000, 95, "fp32"
        ),
        training_run=run,
        parent_version_id=teacher.id,
        derivation="distilled",
    )
    session.add(student)
    await session.flush()
    session.add(
        ModelVersion(
            model_id=student_model.id,
            version=2,
            class_mapping={},
            metrics=_demo_metrics(
                0.85, {"car": 0.93, "pedestrian": 0.84, "sign": 0.69}, 19_800_000, 41, "int8"
            ),
            training_run=run,
            parent_version_id=student.id,
            derivation="quantized",
        )
    )
    print(
        f"Registered a demo model family: {DEMO_TEACHER_MODEL_NAME!r} v1 → distilled "
        f"{DEMO_STUDENT_MODEL_NAME!r} v1 → quantized v2 (illustrative metrics)"
    )


async def _seed(count: int | None, admin_email: str | None, admin_password: str | None) -> int:
    if admin_email is not None and email_domain(admin_email) is None:
        print(f"Not an e-mail address: {admin_email!r}", file=sys.stderr)
        return 2
    available = len(sample_images())
    if available == 0:
        print(f"No sample images found in {SAMPLES_DIR}", file=sys.stderr)
        return 2
    if count is None or count > available:
        if count is not None:
            print(f"Only {available} sample image(s) are bundled; uploading all of them.")
        count = available

    sessionmaker = get_sessionmaker()

    # Sample paths that must survive the storage cleanup: the bundled set,
    # plus anything a person has already annotated — a seeder must never
    # throw away human work, whatever the object it hangs off looks like.
    keep = {f"{DEMO_PREFIX}{path.name}" for path in sample_images()[:count]}
    async with sessionmaker() as session:
        annotated = await session.scalars(
            select(Item.path)
            .join(Project, Project.id == Item.project_id)
            .where(
                Project.name == DEMO_PROJECT_NAME,
                Item.path.startswith(DEMO_PREFIX),
                exists().where(Annotation.item_id == Item.id),
            )
        )
        preserved = set(annotated) - keep
        keep |= preserved
    if preserved:
        print(
            f"Keeping {len(preserved)} stale sample(s) that already have annotations: "
            + ", ".join(sorted(preserved)[:5])
        )

    uploaded, removed = await _prepare_storage(count, keep=keep)

    async with sessionmaker() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == DEMO_ORG_SLUG))
        if org is None:
            org = Organization(name="Default", slug=DEMO_ORG_SLUG)
            session.add(org)
            await session.flush()

        if admin_email:
            user = await session.scalar(select(User).where(User.email == admin_email))
            if user is None:
                if not admin_password:
                    print(
                        "A password is required to create the demo user.",
                        file=sys.stderr,
                    )
                    return 2
                user = User(
                    organization_id=org.id,
                    email=admin_email,
                    display_name=admin_email.split("@", 1)[0],
                    password_hash=hash_password(admin_password),
                    is_active=True,
                    is_superuser=True,
                )
                session.add(user)
                await session.flush()
                print(f"Created superuser {admin_email!r}")
        else:
            user = await session.scalar(select(User).where(User.organization_id == org.id).limit(1))

        connector = await session.scalar(
            select(Connector).where(
                Connector.organization_id == org.id, Connector.name == "Azurite (demo)"
            )
        )
        if connector is None:
            connector = Connector(
                organization_id=org.id,
                name="Azurite (demo)",
                type=ConnectorType.AZURE_BLOB,
                identity_type=ConnectorIdentity.ACCOUNT_KEY,
                # A reference, never the secret: resolved from the environment
                # at use time, exactly as a Key Vault reference would be.
                secret_ref="AZURITE_ACCOUNT_KEY",
                config={
                    "account_url": AZURITE_INTERNAL_URL,
                    # What the BROWSER must use. Without this, signed URLs point
                    # at the compose service name and no image ever loads.
                    "public_account_url": AZURITE_PUBLIC_URL,
                    "account_name": AZURITE_ACCOUNT,
                    "container": DEMO_CONTAINER,
                    "identity_type": "account_key",
                },
            )
            session.add(connector)
            await session.flush()
            print("Created connector 'Azurite (demo)'")

        project = await session.scalar(
            select(Project).where(
                Project.organization_id == org.id, Project.name == DEMO_PROJECT_NAME
            )
        )
        if project is None:
            project = Project(
                organization_id=org.id,
                name=DEMO_PROJECT_NAME,
                description="Seeded by `python -m app.demo seed`. Safe to delete.",
                source_connector_id=connector.id,
                result_connector_id=connector.id,
                source_prefix=DEMO_PREFIX,
                workflow={},
                settings={},
            )
            session.add(project)
            await session.flush()
            print(f"Created project {DEMO_PROJECT_NAME!r}")

        # Deliberately outside the "new project" branch: a project that lost its
        # schema (or predates one) gets it on the next seed. Nesting this inside
        # the create meant re-seeding an existing project silently left it with
        # no label schema and nothing to annotate with.
        if project.label_schema_id is None:
            schema = LabelSchema(project_id=project.id, name="Traffic objects")
            session.add(schema)
            await session.flush()
            session.add(
                LabelSchemaVersion(label_schema_id=schema.id, version=1, definition=DEMO_SCHEMA)
            )
            project.label_schema_id = schema.id
            print(f"Created label schema with {len(DEMO_SCHEMA['classes'])} classes")

        # Register the compose model service once, with the mapping that makes
        # its output land in the project's schema (ML-1, BYOM-2).
        model = await session.scalar(
            select(Model).where(
                Model.organization_id == org.id,
                Model.name == DEMO_MODEL_NAME,
                Model.deleted_at.is_(None),
            )
        )
        if model is None:
            model = Model(
                organization_id=org.id,
                name=DEMO_MODEL_NAME,
                task=ModelTask.DETECT,
                endpoint_url=DEMO_MODEL_URL,
                identity_type="none",
                secret_ref=None,
            )
            session.add(model)
            await session.flush()
            session.add(
                ModelVersion(
                    model_id=model.id, version=1, class_mapping=dict(DEMO_CLASS_MAPPING), metrics={}
                )
            )
            print(f"Registered model {DEMO_MODEL_NAME!r} v1 with mapping {DEMO_CLASS_MAPPING}")
        segmenter = await session.scalar(
            select(Model).where(
                Model.organization_id == org.id,
                Model.name == DEMO_SEGMENT_MODEL_NAME,
                Model.deleted_at.is_(None),
            )
        )
        if segmenter is None:
            session.add(
                Model(
                    organization_id=org.id,
                    name=DEMO_SEGMENT_MODEL_NAME,
                    task=ModelTask.SEGMENT,
                    endpoint_url=DEMO_MODEL_URL,
                    identity_type="none",
                    secret_ref=None,
                )
            )
            print(
                f"Registered segment model {DEMO_SEGMENT_MODEL_NAME!r} for the smart polygon tool"
            )

        await _seed_model_family(session, org.id)

        # Items whose objects were just deleted from storage. The scan below
        # only creates and updates; it never notices an object has gone.
        if removed:
            gone = await session.scalars(
                delete(Item)
                .where(Item.project_id == project.id, Item.path.in_(removed))
                .returning(Item.id)
            )
            if count_gone := len(list(gone)):
                print(f"Removed {count_gone} item(s) for stale sample objects")

        if user is not None:
            membership = await session.scalar(
                select(Membership).where(
                    Membership.project_id == project.id, Membership.user_id == user.id
                )
            )
            if membership is None:
                session.add(
                    Membership(user_id=user.id, project_id=project.id, role=ProjectRole.OWNER)
                )

        await session.commit()
        project_id: UUID = project.id
        connector_row = connector
        org_id: UUID = org.id
        owner_id: UUID | None = user.id if user is not None else None

    # Scan for real, through the same code path the API uses.
    async with sessionmaker() as session:
        from app.connectors.registry import build_connector

        storage = build_connector(
            connector_row.type.value,
            dict(connector_row.config),
            AZURITE_KEY,
        )
        try:
            result = await scan_source(
                session,
                project_id=project_id,
                connector=connector_row,
                storage=storage,
                prefix=DEMO_PREFIX,
            )
            await session.commit()
        finally:
            await storage.aclose()

    print(
        f"Scan complete: {result.scanned} object(s) seen, "
        f"{result.created} item(s) created, {result.updated} updated, "
        f"{result.skipped} skipped."
    )
    if result.errors:
        print("Warnings:")
        for message in result.errors[:5]:
            print(f"  - {message}")

    # LLM evaluation items (§5 LLM-data) in the same container.
    from app.demo_llm import seed_llm_project

    await seed_llm_project(org_id, owner_id, connector_row, AZURITE_KEY)

    # Audio and time-series items (§5).
    from app.demo_segments import seed_segments_project

    await seed_segments_project(org_id, owner_id, connector_row, AZURITE_KEY)

    # Image + caption tasks (§5 multimodal).
    from app.demo_multimodal import seed_multimodal_project

    await seed_multimodal_project(org_id, owner_id, connector_row, AZURITE_KEY)

    # PDF documents, in layout mode and in text mode (TOOL).
    from app.demo_pdf import seed_pdf_projects

    await seed_pdf_projects(org_id, owner_id, connector_row, AZURITE_KEY)

    print(f"\nUploaded {uploaded} image(s). Open the project at http://localhost:5173/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.demo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Create demo storage, a project and items")
    seed.add_argument(
        "--count",
        type=int,
        default=None,
        help="How many of the bundled sample images to upload (default: all)",
    )
    seed.add_argument("--admin-email", default=None, help="Also create this superuser")
    seed.add_argument("--admin-password", default=None)

    sub.add_parser(
        "seed-emulators",
        help="Add demo projects on the S3 and GCS emulators (compose --profile emulators)",
    )

    args = parser.parse_args(argv)
    if args.command == "seed":
        return asyncio.run(_run_seed(args))
    if args.command == "seed-emulators":
        return asyncio.run(_run_seed_emulators())
    return 1


async def _run_seed(args: argparse.Namespace) -> int:
    """Run the seed and dispose the engine inside ONE event loop.

    Disposing in a second `asyncio.run` closes connections belonging to a loop
    that has already gone, which surfaces as `RuntimeError: Event loop is
    closed` after the work has actually succeeded.
    """
    try:
        return await _seed(args.count, args.admin_email, args.admin_password)
    finally:
        await get_engine().dispose()


async def _run_seed_emulators() -> int:
    from app.demo_emulators import seed_emulators

    try:
        return await seed_emulators()
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    raise SystemExit(main())
