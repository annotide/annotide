"""Demo projects on the AWS and Google Cloud emulators (`--profile emulators`).

Run after ``python -m app.demo seed``; it reuses that organisation, label
schema and sample photos:

    python -m app.demo seed-emulators

For each emulated cloud it creates a bucket with the CORS rule a browser
needs, uploads the samples through the connector itself, and adds a
connector and a project scanned from it. The result is the Azure demo's full
loop running on S3 (Moto) and GCS (fake-gcs-server). It also registers a
webhook per format (signed JSON, Slack, Teams, API-7) to the compose echo
receiver, so every event can be read in ``docker compose logs webhook-sink``.
Idempotent, like the main seed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.connectors.registry import build_connector
from app.db.session import get_sessionmaker
from app.demo import DEMO_ORG_SLUG, DEMO_PREFIX, DEMO_SCHEMA, demo_cors_origins, sample_images
from app.models import (
    Connector,
    ConnectorIdentity,
    ConnectorType,
    LabelSchema,
    LabelSchemaVersion,
    Membership,
    Organization,
    Project,
    ProjectRole,
    User,
    Webhook,
    WebhookFormat,
)
from app.services.scanning import scan_source
from app.services.webhooks import generate_secret

BUCKET = "media"
REGION = "eu-west-1"
#: Inside the compose network (backend, worker) and from the browser.
S3_INTERNAL_URL = os.environ.get("S3_EMULATOR_INTERNAL_URL", "http://s3:5000")
S3_PUBLIC_URL = os.environ.get("S3_EMULATOR_PUBLIC_URL", "http://localhost:9100")
GCS_INTERNAL_URL = os.environ.get("GCS_EMULATOR_INTERNAL_URL", "http://gcs:4443")
GCS_PUBLIC_URL = os.environ.get("GCS_EMULATOR_PUBLIC_URL", "http://localhost:4443")
WEBHOOK_SINK_URL = os.environ.get("WEBHOOK_SINK_INTERNAL_URL", "http://webhook-sink:8080")

#: Env var holding the S3 key pair; set for backend and worker in compose.
#: Moto accepts any pair, so this is no secret.
S3_SECRET_REF = "EMULATOR_S3_CREDENTIALS"
S3_CREDENTIALS = {"access_key_id": "test", "secret_access_key": "test"}

WEBHOOK_DESCRIPTION = "Compose echo receiver (seed-emulators)"


@dataclass(frozen=True)
class EmulatedStore:
    connector_name: str
    project_name: str
    type: ConnectorType
    identity: ConnectorIdentity
    secret_ref: str | None
    secret: str | None
    config: dict[str, Any] = field(default_factory=dict)


def stores() -> tuple[EmulatedStore, ...]:
    """The emulated stores, with the endpoints as currently configured."""
    return (
        EmulatedStore(
            connector_name="S3 (Moto emulator)",
            project_name="Demo on S3: traffic objects",
            type=ConnectorType.S3,
            identity=ConnectorIdentity.ACCESS_KEY,
            secret_ref=S3_SECRET_REF,
            secret=json.dumps(S3_CREDENTIALS),
            config={
                "bucket": BUCKET,
                "identity_type": "access_key",
                "region": REGION,
                "endpoint_url": S3_INTERNAL_URL,
                "public_endpoint_url": S3_PUBLIC_URL,
                "force_path_style": True,
            },
        ),
        EmulatedStore(
            connector_name="GCS (fake-gcs-server emulator)",
            project_name="Demo on GCS: traffic objects",
            type=ConnectorType.GCS,
            # Anonymous: nothing to sign with, so browser uploads are refused here.
            identity=ConnectorIdentity.NONE,
            secret_ref=None,
            secret=None,
            config={
                "bucket": BUCKET,
                "identity_type": "none",
                "project": "local",
                "endpoint_url": GCS_INTERNAL_URL,
                "public_endpoint_url": GCS_PUBLIC_URL,
            },
        ),
    )


async def _create_s3_bucket(origins: list[str]) -> None:
    import aioboto3  # type: ignore[import-untyped]  # no inline type stubs shipped

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=S3_INTERNAL_URL,
        region_name=REGION,
        aws_access_key_id=S3_CREDENTIALS["access_key_id"],
        aws_secret_access_key=S3_CREDENTIALS["secret_access_key"],
    ) as client:
        with contextlib.suppress(client.exceptions.BucketAlreadyOwnedByYou):
            await client.create_bucket(
                Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        rule = {"AllowedOrigins": origins, "AllowedMethods": ["GET", "HEAD", "PUT"]}
        await client.put_bucket_cors(
            Bucket=BUCKET,
            CORSConfiguration={"CORSRules": [{**rule, "AllowedHeaders": ["*"]}]},
        )


async def _create_gcs_bucket() -> None:
    # fake-gcs-server answers every origin itself and keeps no bucket CORS.
    from google.api_core.exceptions import Conflict
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    def create() -> None:
        client = storage.Client(
            project="local",
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]  # untyped
            client_options={"api_endpoint": GCS_INTERNAL_URL},
        )
        with contextlib.suppress(Conflict):
            client.create_bucket(BUCKET)

    await asyncio.to_thread(create)


async def _seed_store(store: EmulatedStore, org: Organization, owner: User | None) -> None:
    storage = build_connector(store.type.value, dict(store.config), store.secret)
    try:
        for path in sample_images():
            await storage.write(f"{DEMO_PREFIX}{path.name}", path.read_bytes(), "image/jpeg")
        print(f"Uploaded {len(sample_images())} sample image(s) to {store.connector_name}")

        async with get_sessionmaker()() as session:
            connector = await session.scalar(
                select(Connector).where(
                    Connector.organization_id == org.id,
                    Connector.name == store.connector_name,
                )
            )
            if connector is None:
                connector = Connector(
                    organization_id=org.id,
                    name=store.connector_name,
                    type=store.type,
                    identity_type=store.identity,
                    secret_ref=store.secret_ref,
                    config=dict(store.config),
                )
                session.add(connector)
                await session.flush()
                print(f"Created connector {store.connector_name!r}")

            project = await session.scalar(
                select(Project).where(
                    Project.organization_id == org.id, Project.name == store.project_name
                )
            )
            if project is None:
                project = Project(
                    organization_id=org.id,
                    name=store.project_name,
                    description="Seeded by `python -m app.demo seed-emulators`. Safe to delete.",
                    source_connector_id=connector.id,
                    result_connector_id=connector.id,
                    source_prefix=DEMO_PREFIX,
                    workflow={},
                    settings={},
                )
                session.add(project)
                await session.flush()
                print(f"Created project {store.project_name!r}")
            if project.label_schema_id is None:
                schema = LabelSchema(project_id=project.id, name="Traffic objects")
                session.add(schema)
                await session.flush()
                session.add(
                    LabelSchemaVersion(label_schema_id=schema.id, version=1, definition=DEMO_SCHEMA)
                )
                project.label_schema_id = schema.id
            if owner is not None and not await session.scalar(
                select(Membership).where(
                    Membership.project_id == project.id, Membership.user_id == owner.id
                )
            ):
                session.add(
                    Membership(user_id=owner.id, project_id=project.id, role=ProjectRole.OWNER)
                )

            result = await scan_source(
                session,
                project_id=project.id,
                connector=connector,
                storage=storage,
                prefix=DEMO_PREFIX,
            )
            await session.commit()
        print(f"  scan: {result.created} item(s) created, {result.updated} updated")
        for message in result.errors[:5]:
            print(f"  - {message}")
    finally:
        await storage.aclose()


#: One hook per format, all to the echo receiver: (description, path, format).
WEBHOOKS = (
    (WEBHOOK_DESCRIPTION, "annotation-events", WebhookFormat.JSON),
    ("Compose echo receiver, Slack format (seed-emulators)", "slack", WebhookFormat.SLACK),
    ("Compose echo receiver, Teams format (seed-emulators)", "teams", WebhookFormat.TEAMS),
)


async def _seed_webhook(org: Organization, owner: User | None) -> None:
    async with get_sessionmaker()() as session:
        for description, path, hook_format in WEBHOOKS:
            hook = await session.scalar(
                select(Webhook).where(
                    Webhook.organization_id == org.id, Webhook.description == description
                )
            )
            if hook is not None:
                continue
            session.add(
                Webhook(
                    organization_id=org.id,
                    url=f"{WEBHOOK_SINK_URL}/{path}",
                    description=description,
                    events=["*"],
                    secret=generate_secret(),
                    is_active=True,
                    format=hook_format,
                    created_by_id=owner.id if owner is not None else None,
                )
            )
            print(f"Registered a {hook_format.value} webhook for every event to the echo receiver")
        await session.commit()
    print("  deliveries: docker compose logs -f webhook-sink")


async def seed_emulators() -> int:
    if not sample_images():
        print("No sample images are bundled.", file=sys.stderr)
        return 2
    async with get_sessionmaker()() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == DEMO_ORG_SLUG))
        if org is None:
            print("Run `python -m app.demo seed` (make seed) first.", file=sys.stderr)
            return 2
        owner = await session.scalar(
            select(User)
            .where(User.organization_id == org.id, User.is_superuser.is_(True))
            .order_by(User.created_at)
            .limit(1)
        )

    await _create_s3_bucket(demo_cors_origins())
    await _create_gcs_bucket()
    for store in stores():
        await _seed_store(store, org, owner)
    await _seed_webhook(org, owner)
    print("\nOpen the projects at http://localhost:5173/")
    return 0
