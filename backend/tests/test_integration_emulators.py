"""Connectors, secrets and webhooks against local cloud emulators.

Skipped unless ``RUN_INTEGRATION=1``: they need the compose emulators running
(``make emulators``), and ``make integration`` starts those and runs this file.
Unlike the unit tests, nothing here is mocked. Each connector talks to a real
storage API, and every signed URL is fetched the way a browser fetches it:
plain HTTP with an ``Origin`` header, with no SDK involved (ARC-3, SRC-7).

    Azure Blob  → Azurite              ${AZURITE_URL:-http://localhost:10000}
    AWS S3      → Moto                 ${S3_EMULATOR_URL:-http://localhost:9100}
    AWS Secrets → Moto (same server)
    GCS         → fake-gcs-server      ${GCS_EMULATOR_URL:-http://localhost:4443}
    Webhooks    → http-https-echo      ${WEBHOOK_SINK_URL:-http://localhost:8099}
    E-mail      → Mailpit              ${MAILPIT_URL:-http://localhost:8025}, SMTP :1025
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.compiler import compiles

from app.connectors.base import StorageConnector
from app.connectors.errors import ConnectorNotFound, UnsupportedOperation
from app.connectors.registry import build_connector
from app.models import Webhook, WebhookDelivery
from app.services import webhooks
from app.services.secrets.backends import AwsSecretsBackend
from app.services.secrets.base import parse_ref


@compiles(CITEXT, "sqlite")  # pragma: no cover - the seed test's SQLite schema
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="needs the compose emulators: `make integration`",
)

ORIGIN = "http://localhost:5173"
BUCKET = "integration"
REGION = "eu-west-1"
AZURITE_URL = os.environ.get("AZURITE_URL", "http://localhost:10000").rstrip("/")
S3_URL = os.environ.get("S3_EMULATOR_URL", "http://localhost:9100").rstrip("/")
GCS_URL = os.environ.get("GCS_EMULATOR_URL", "http://localhost:4443").rstrip("/")
SINK_URL = os.environ.get("WEBHOOK_SINK_URL", "http://localhost:8099").rstrip("/")
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://localhost:8025").rstrip("/")
MAILPIT_SMTP = os.environ.get("MAILPIT_SMTP", "localhost:1025")

# Well-known emulator credentials (Microsoft's documented Azurite key, Moto's
# accept-anything pair). They open nothing but a local emulator.
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
MOTO_CREDENTIALS = {"access_key_id": "test", "secret_access_key": "test"}


async def _azure() -> StorageConnector:
    from azure.storage.blob import CorsRule
    from azure.storage.blob.aio import BlobServiceClient

    account_url = f"{AZURITE_URL}/{AZURITE_ACCOUNT}"
    connection = (
        f"DefaultEndpointsProtocol=http;AccountName={AZURITE_ACCOUNT};"
        f"AccountKey={AZURITE_KEY};BlobEndpoint={account_url};"
    )
    async with BlobServiceClient.from_connection_string(connection) as client:
        rule = CorsRule([ORIGIN], ["GET", "HEAD", "PUT", "OPTIONS"], allowed_headers=["*"])
        await client.set_service_properties(cors=[rule])
        with contextlib.suppress(Exception):  # already exists on a re-run
            await client.create_container(BUCKET)
    config = {
        "account_url": account_url,
        "account_name": AZURITE_ACCOUNT,
        "container": BUCKET,
        "identity_type": "account_key",
        "frontend_origin": ORIGIN,
    }
    connector: StorageConnector = build_connector("azure_blob", config, AZURITE_KEY)
    return connector


async def _s3() -> StorageConnector:
    import aioboto3  # type: ignore[import-untyped]  # no inline type stubs shipped

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=S3_URL,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    ) as client:
        with contextlib.suppress(Exception):  # already exists on a re-run
            await client.create_bucket(
                Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        rule = {"AllowedOrigins": [ORIGIN], "AllowedMethods": ["GET", "HEAD", "PUT"]}
        await client.put_bucket_cors(
            Bucket=BUCKET,
            CORSConfiguration={"CORSRules": [{**rule, "AllowedHeaders": ["*"]}]},
        )
    config = {
        "bucket": BUCKET,
        "identity_type": "access_key",
        "region": REGION,
        "endpoint_url": S3_URL,
        "force_path_style": True,
        "frontend_origin": ORIGIN,
    }
    connector: StorageConnector = build_connector("s3", config, json.dumps(MOTO_CREDENTIALS))
    return connector


async def _gcs() -> StorageConnector:
    import asyncio

    from google.api_core.exceptions import Conflict
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    def create() -> None:
        client = storage.Client(
            project="local",
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]  # untyped
            client_options={"api_endpoint": GCS_URL},
        )
        with contextlib.suppress(Conflict):
            client.create_bucket(BUCKET)

    await asyncio.to_thread(create)
    config = {
        "bucket": BUCKET,
        "identity_type": "none",
        "project": "local",
        "endpoint_url": GCS_URL,
    }
    connector: StorageConnector = build_connector("gcs", config, None)
    return connector


STORES: dict[str, Callable[[], Any]] = {"azure_blob": _azure, "s3": _s3, "gcs": _gcs}


@pytest.fixture(params=list(STORES))
async def store(request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, StorageConnector]]:
    connector: StorageConnector = await STORES[request.param]()
    try:
        yield request.param, connector
    finally:
        await connector.aclose()


@pytest.fixture
def prefix() -> str:
    """A fresh prefix per test, so runs never see each other's objects."""
    return f"it-{uuid.uuid4().hex[:12]}/"


async def test_check_reaches_the_bucket(store: tuple[str, StorageConnector]) -> None:
    name, connector = store
    result = await connector.check()
    assert result.ok, result.messages
    if name != "gcs":  # fake-gcs-server does not keep a bucket's CORS rules
        assert any("CORS allows" in message for message in result.messages), result.messages


async def test_write_list_read_delete(store: tuple[str, StorageConnector], prefix: str) -> None:
    _, connector = store
    path = f"{prefix}images/cat.jpg"
    await connector.write(path, b"0123456789", content_type="image/jpeg")
    await connector.write(f"{prefix}notes.txt", b"hi", content_type="text/plain")

    listed = [info.path async for info in connector.list(prefix, glob="*.jpg")]
    assert listed == [path]
    assert await connector.read(path) == b"0123456789"
    assert await connector.read(path, start=2, end=5) == b"234"  # end is exclusive

    await connector.delete(path)
    with pytest.raises(ConnectorNotFound):
        await connector.read(path)


async def test_the_browser_can_read_a_signed_url(
    store: tuple[str, StorageConnector], prefix: str
) -> None:
    _, connector = store
    path = f"{prefix}photo 1.jpg"
    await connector.write(path, b"jpeg bytes", content_type="image/jpeg")

    url = await connector.signed_url(path, expires_in=300)
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert response.content == b"jpeg bytes"
    # Without it the annotator's canvas stays empty (SRC-7).
    assert response.headers.get("access-control-allow-origin") in {ORIGIN, "*"}


async def test_the_browser_can_upload_to_a_signed_url(
    store: tuple[str, StorageConnector], prefix: str
) -> None:
    name, connector = store
    path = f"{prefix}upload.png"
    if name == "gcs":  # anonymous: nothing to sign an upload with
        with pytest.raises(UnsupportedOperation):
            await connector.signed_url(path, write=True)
        return

    url = await connector.signed_url(path, expires_in=300, write=True)
    headers = {"Origin": ORIGIN, "Content-Type": "image/png"}
    if name == "azure_blob":
        headers["x-ms-blob-type"] = "BlockBlob"
    async with httpx.AsyncClient() as client:
        preflight = await client.options(
            url,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 200, preflight.text
        response = await client.put(url, content=b"png bytes", headers=headers)
    assert response.status_code in {200, 201}, response.text
    assert await connector.read(path) == b"png bytes"


async def test_s3_credentials_from_aws_secrets_manager(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """An `awssecrets://` reference resolves through Moto into a working connector."""
    import boto3

    monkeypatch.setenv("AWS_ENDPOINT_URL_SECRETS_MANAGER", S3_URL)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    name = f"integration/{prefix.strip('/')}"
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name=name, SecretString=json.dumps(MOTO_CREDENTIALS)
    )

    backend = AwsSecretsBackend(default_region=REGION)
    try:
        secret = await backend.get(parse_ref(f"awssecrets://{name}"))
        key_id = await backend.get(parse_ref(f"awssecrets://{name}?key=access_key_id"))
    finally:
        await backend.aclose()
    assert json.loads(secret) == MOTO_CREDENTIALS
    assert key_id == "test"

    connector = await _s3()
    try:
        assert (await connector.check()).ok
    finally:
        await connector.aclose()


async def test_a_webhook_arrives_signed() -> None:
    """A real delivery to a real receiver; the echo shows what it got."""
    hook = Webhook(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        url=f"{SINK_URL}/hooks/annotation",
        secret=webhooks.seal_secret(webhooks.generate_secret()),
        events=["*"],
        is_active=True,
    )
    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        webhook_id=hook.id,
        event="item.submitted",
        payload={"event": "item.submitted", "data": {"item_id": "i-1"}},
        attempts=0,
    )
    echoed: dict[str, Any] = {}

    async def capture(response: httpx.Response) -> None:
        await response.aread()
        echoed.update(response.json())

    async with httpx.AsyncClient(event_hooks={"response": [capture]}) as client:
        ok = await webhooks.deliver(delivery, hook, client, max_attempts=3, now=datetime.now(UTC))

    assert ok, delivery.error
    assert echoed["path"] == "/hooks/annotation"
    received = {key.lower(): value for key, value in echoed["headers"].items()}
    assert received["x-annotation-event"] == "item.submitted"
    body = echoed["body"].encode()
    assert json.loads(body)["delivery_id"] == str(delivery.id)
    assert webhooks.verify(webhooks.signing_secret(hook), received["x-annotation-signature"], body)


async def test_seed_emulators_builds_working_projects_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make seed-emulators`, on SQLite and the host's emulator ports."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app import demo, demo_emulators
    from app.db.base import Base
    from app.models import Connector, Item, Organization, Project, User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sessionmaker() as session:
        org = Organization(name="Default", slug=demo.DEMO_ORG_SLUG)
        session.add(org)
        await session.flush()
        session.add(
            User(
                organization_id=org.id,
                email="admin@example.com",
                display_name="admin",
                password_hash="x",
                is_active=True,
                is_superuser=True,
            )
        )
        await session.commit()

    monkeypatch.setattr(demo_emulators, "get_sessionmaker", lambda: sessionmaker)
    for name, url in {
        "S3_INTERNAL_URL": S3_URL,
        "S3_PUBLIC_URL": S3_URL,
        "GCS_INTERNAL_URL": GCS_URL,
        "GCS_PUBLIC_URL": GCS_URL,
    }.items():
        monkeypatch.setattr(demo_emulators, name, url)
    try:
        assert await demo_emulators.seed_emulators() == 0
        assert await demo_emulators.seed_emulators() == 0  # a re-run adds nothing

        async with sessionmaker() as session:
            projects = list(
                await session.scalars(select(Project).where(Project.name.startswith("Demo on")))
            )
            assert sorted(project.name for project in projects) == [
                "Demo on GCS: traffic objects",
                "Demo on S3: traffic objects",
            ]
            samples = len(demo.sample_images())
            for project in projects:
                count = await session.scalar(
                    select(func.count()).select_from(Item).where(Item.project_id == project.id)
                )
                assert count == samples
            assert await session.scalar(select(func.count()).select_from(Connector)) == 2
            hooks = list(await session.scalars(select(Webhook)))
            assert sorted((hook.format.value, hook.url) for hook in hooks) == [
                ("json", f"{demo_emulators.WEBHOOK_SINK_URL}/annotation-events"),
                ("slack", f"{demo_emulators.WEBHOOK_SINK_URL}/slack"),
                ("teams", f"{demo_emulators.WEBHOOK_SINK_URL}/teams"),
            ]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("hook_format", ["slack", "teams"])
async def test_a_chat_webhook_arrives_as_a_chat_message(hook_format: str) -> None:
    """API-7: Slack / Teams bodies, as the receiver saw them."""
    from app.models import WebhookFormat

    project_id = uuid.uuid4()
    hook = Webhook(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        url=f"{SINK_URL}/{hook_format}",
        secret=webhooks.seal_secret(webhooks.generate_secret()),
        events=["*"],
        is_active=True,
        format=WebhookFormat(hook_format),
    )
    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        webhook_id=hook.id,
        event="annotation.rejected",
        payload={
            "event": "annotation.rejected",
            "project_id": str(project_id),
            "data": {"item_id": "i-1", "item_path": "a/b.jpg", "comment": "Missed a <car>"},
        },
        attempts=0,
    )
    echoed: dict[str, Any] = {}

    async def capture(response: httpx.Response) -> None:
        await response.aread()
        echoed.update(response.json())

    async with httpx.AsyncClient(event_hooks={"response": [capture]}) as client:
        ok = await webhooks.deliver(
            delivery,
            hook,
            client,
            max_attempts=3,
            project_name="Street scenes",
            frontend_url="http://localhost:5173",
        )

    assert ok, delivery.error
    body = json.loads(echoed["body"])
    link = f"http://localhost:5173/projects/{project_id}/annotate/i-1"
    if hook_format == "slack":
        assert body["text"] == "Street scenes: Annotation rejected for a/b.jpg"
        assert "&lt;car&gt;" in body["blocks"][0]["text"]["text"]
        assert link in body["blocks"][1]["elements"][0]["text"]
    else:
        card = body["attachments"][0]["content"]
        assert [block["text"] for block in card["body"]] == [
            "Street scenes",
            "Annotation rejected for a/b.jpg",
            "Missed a <car>",
        ]
        assert card["actions"][0]["url"] == link


async def test_notification_email_goes_through_smtp() -> None:
    """API-7: a real SMTP conversation with Mailpit, read back over its API."""
    from app.core.config import Settings
    from app.models import Notification, NotificationType, User
    from app.services.notification_email import SmtpMailer, compose

    host, port = MAILPIT_SMTP.rsplit(":", 1)
    settings = Settings(
        database_url="postgresql+asyncpg://t:t@localhost/t",
        secret_key="test-key",
        smtp_host=host,
        smtp_port=int(port),
        smtp_security="none",
        smtp_from="Annotide <noreply@integration.test>",
        frontend_url="http://localhost:5173",
    )
    marker = uuid.uuid4().hex[:8]
    notification = Notification(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type=NotificationType.MENTION,
        payload={"project_id": "p", "item_id": "i", "actor_id": "a", "excerpt": marker},
    )
    recipient = User(email=f"anna-{marker}@integration.test", display_name="Anna")
    message = compose(
        notification, recipient, actor="Bob", project="Street scenes", settings=settings
    )

    assert await SmtpMailer(settings).send([message]) == [None]

    async with httpx.AsyncClient() as client:
        found = (
            await client.get(
                f"{MAILPIT_URL}/api/v1/search", params={"query": f"to:{recipient.email}"}
            )
        ).json()
        assert found["messages_count"] == 1
        stored = (
            await client.get(f"{MAILPIT_URL}/api/v1/message/{found['messages'][0]['ID']}")
        ).json()
    assert stored["Subject"] == "Bob mentioned you in Street scenes"
    assert f"> {marker}" in stored["Text"]
