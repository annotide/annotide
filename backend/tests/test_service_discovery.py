"""Tests for `services/discovery.py`: storage notifications → paths per project (SRC-3).

The bodies below follow each sender's documented shape, trimmed to what the
parser reads plus a little of what it must ignore.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Coroutine, Iterator
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import Connector, ConnectorIdentity, ConnectorType, Project
from app.services import discovery
from app.services.discovery import EventFormatError, ObjectChange, ObjectEvent

ORG_ID = uuid.uuid4()

_TABLES = cast("list[Table]", [Connector.__table__, Project.__table__])


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _created(container: str | None, path: str) -> ObjectEvent:
    return ObjectEvent(ObjectChange.CREATED, container, path)


def _deleted(container: str | None, path: str) -> ObjectEvent:
    return ObjectEvent(ObjectChange.DELETED, container, path)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _grid_event(kind: str, container: str, blob: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "eventType": kind,
        "subject": f"/blobServices/default/containers/{container}/blobs/{blob}",
        "eventTime": "2026-09-28T10:00:00Z",
        "data": {"api": "PutBlob", "contentLength": 12, "eTag": "0x8D"},
        "dataVersion": "",
    }


class TestEventGrid:
    def test_blob_created_and_deleted_name_container_and_path(self) -> None:
        delivery = discovery.parse_delivery(
            [
                _grid_event("Microsoft.Storage.BlobCreated", "media", "cams/north/a.jpg"),
                _grid_event("Microsoft.Storage.BlobDeleted", "media", "old.jpg"),
                _grid_event("Microsoft.Storage.BlobTierChanged", "media", "cold.jpg"),
            ]
        )
        assert delivery.events == [
            _created("media", "cams/north/a.jpg"),
            _deleted("media", "old.jpg"),
        ]

    def test_subscription_validation_hands_back_the_code(self) -> None:
        delivery = discovery.parse_delivery(
            [
                {
                    "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                    "subject": "",
                    "data": {"validationCode": "512d38b6-c7b8-40c8-89fe-f46f9e9622b6"},
                }
            ]
        )
        assert delivery.validation_code == "512d38b6-c7b8-40c8-89fe-f46f9e9622b6"
        assert delivery.events == []

    def test_validation_without_a_code_is_refused(self) -> None:
        with pytest.raises(EventFormatError):
            discovery.parse_delivery(
                [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {}}]
            )

    def test_cloud_events_single_and_batched(self) -> None:
        event = {
            "specversion": "1.0",
            "type": "Microsoft.Storage.BlobCreated",
            "source": "/subscriptions/x/resourceGroups/y/providers/Microsoft.Storage/"
            "storageAccounts/acme",
            "subject": "/blobServices/default/containers/media/blobs/a b.png",
            "id": "1",
            "data": {},
        }
        assert discovery.parse_delivery(event).events == [_created("media", "a b.png")]
        assert discovery.parse_delivery([event, event]).events == [_created("media", "a b.png")] * 2


def _s3_record(name: str, bucket: str, key: str) -> dict[str, Any]:
    return {
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "eventName": name,
        "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": 12, "eTag": "abc"}},
    }


class TestS3:
    def test_records_are_url_decoded(self) -> None:
        delivery = discovery.parse_delivery(
            {
                "Records": [
                    _s3_record("ObjectCreated:Put", "shots", "day+1/%C3%A4iti.png"),
                    _s3_record("ObjectRemoved:Delete", "shots", "gone.png"),
                    _s3_record("ObjectRestore:Completed", "shots", "thawed.png"),
                ]
            }
        )
        assert delivery.events == [
            _created("shots", "day 1/äiti.png"),
            _deleted("shots", "gone.png"),
        ]

    def test_minio_prefixes_event_names_with_s3(self) -> None:
        body = {
            "EventName": "s3:ObjectCreated:Put",
            "Key": "shots/a.png",
            "Records": [_s3_record("s3:ObjectCreated:Put", "shots", "a.png")],
        }
        assert discovery.parse_delivery(body).events == [_created("shots", "a.png")]

    def test_the_test_event_queues_nothing(self) -> None:
        body = {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "shots"}
        assert discovery.parse_delivery(body).events == []

    def test_through_sns(self) -> None:
        message = {"Records": [_s3_record("ObjectCreated:CompleteMultipartUpload", "b", "v.mp4")]}
        body = {
            "Type": "Notification",
            "MessageId": "m",
            "TopicArn": "arn:aws:sns:eu-west-1:1:uploads",
            "Message": json.dumps(message),
        }
        assert discovery.parse_delivery(body).events == [_created("b", "v.mp4")]

    def test_sns_subscription_confirmation(self) -> None:
        url = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
        body = {"Type": "SubscriptionConfirmation", "SubscribeURL": url, "Token": "t"}
        delivery = discovery.parse_delivery(body)
        assert delivery.subscribe_url == url
        assert delivery.events == []

    def test_sns_message_that_is_not_an_s3_event_is_refused(self) -> None:
        with pytest.raises(EventFormatError):
            discovery.parse_delivery({"Type": "Notification", "Message": "hello"})
        with pytest.raises(EventFormatError):
            discovery.parse_delivery({"Type": "Notification", "Message": '{"alarm": 1}'})

    def test_through_eventbridge_the_key_is_used_as_sent(self) -> None:
        body = {
            "version": "0",
            "source": "aws.s3",
            "detail-type": "Object Created",
            "detail": {"bucket": {"name": "shots"}, "object": {"key": "a+b.png", "size": 3}},
        }
        assert discovery.parse_delivery(body).events == [_created("shots", "a+b.png")]


class TestPubSub:
    def test_gcs_finalize_and_delete(self) -> None:
        def push(kind: str, name: str) -> dict[str, Any]:
            return {
                "message": {
                    "attributes": {"eventType": kind, "bucketId": "frames", "objectId": name},
                    "data": "e30=",
                    "messageId": "1",
                },
                "subscription": "projects/p/subscriptions/s",
            }

        assert discovery.parse_delivery(push("OBJECT_FINALIZE", "x/1.png")).events == [
            _created("frames", "x/1.png")
        ]
        assert discovery.parse_delivery(push("OBJECT_DELETE", "x/1.png")).events == [
            _deleted("frames", "x/1.png")
        ]
        assert discovery.parse_delivery(push("OBJECT_METADATA_UPDATE", "x/1.png")).events == []


@pytest.mark.parametrize("body", [{"hello": "world"}, "text", 3, None])
def test_an_unknown_shape_is_refused(body: Any) -> None:
    with pytest.raises(EventFormatError):
        discovery.parse_delivery(body)


# --------------------------------------------------------------------------- #
# Token and SNS
# --------------------------------------------------------------------------- #


def _connector(config: dict[str, Any] | None = None) -> Connector:
    return Connector(
        id=uuid.uuid4(),
        organization_id=ORG_ID,
        name="store",
        type=ConnectorType.S3,
        identity_type=ConnectorIdentity.NONE,
        config=config if config is not None else {"bucket": "shots"},
    )


def test_a_token_matches_only_its_own_hash() -> None:
    token, digest = discovery.new_event_token()
    connector = _connector()
    assert not discovery.token_matches(connector, token)  # events off

    connector.event_token_hash = digest
    assert token.startswith("evt_")
    assert digest != token
    assert discovery.token_matches(connector, token)
    assert not discovery.token_matches(connector, token + "x")
    assert not discovery.token_matches(connector, None)
    assert not discovery.token_matches(connector, "")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription", True),
        ("https://sns.cn-north-1.amazonaws.com.cn/?Action=ConfirmSubscription", True),
        ("http://sns.eu-west-1.amazonaws.com/", False),
        ("https://sns.eu-west-1.amazonaws.com.evil.example/", False),
        ("https://evil.example/sns.eu-west-1.amazonaws.com/", False),
        ("https://user@sns.eu-west-1.amazonaws.com/", False),
        ("https://sns.eu-west-1.amazonaws.com:8443/", False),
        ("https://169.254.169.254/latest/meta-data/", False),
    ],
)
def test_only_an_sns_endpoint_is_fetched(url: str, ok: bool) -> None:
    assert discovery.is_sns_url(url) is ok


def test_confirming_a_subscription_gets_the_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<ConfirmSubscriptionResponse/>")

    url = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
    _run(discovery.confirm_sns_subscription(url, transport=httpx.MockTransport(handler)))
    assert seen == [url]


def test_confirming_refuses_a_foreign_url_before_fetching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"fetched {request.url}")

    with pytest.raises(EventFormatError):
        _run(
            discovery.confirm_sns_subscription(
                "https://example.com/confirm", transport=httpx.MockTransport(handler)
            )
        )


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


@pytest.fixture
def sessionmaker() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TABLES)

    _run(_create())
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    _run(engine.dispose())


def _route(
    sessionmaker: async_sessionmaker[AsyncSession],
    connector: Connector,
    projects: list[dict[str, Any]],
    events: list[ObjectEvent],
) -> tuple[dict[uuid.UUID, list[str]], list[uuid.UUID]]:
    async def _go() -> tuple[dict[uuid.UUID, list[str]], list[uuid.UUID]]:
        async with sessionmaker() as session:
            session.add(connector)
            await session.flush()
            rows = [
                Project(
                    organization_id=ORG_ID,
                    name=f"p{n}",
                    settings={},
                    workflow={},
                    **{"source_connector_id": connector.id, **fields},
                )
                for n, fields in enumerate(projects)
            ]
            session.add_all(rows)
            await session.commit()
            routed = await discovery.route_events(session, connector, events)
            return routed, [row.id for row in rows]

    return _run(_go())


class TestRouting:
    def test_each_project_gets_what_its_prefix_and_glob_select(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        routed, (north, pngs, everything) = _route(
            sessionmaker,
            _connector(),
            [
                {"source_prefix": "cams/north/"},
                {"source_prefix": "", "source_glob": "*.png"},
                {},
            ],
            [
                _created("shots", "cams/north/a.jpg"),
                _created("shots", "cams/south/b.png"),
                _created("shots", "cams/north/a.jpg"),  # delivered twice
                _created("shots", "README"),  # no supported extension
            ],
        )
        assert routed == {
            north: ["cams/north/a.jpg"],
            pngs: ["cams/south/b.png"],
            everything: ["cams/north/a.jpg", "cams/south/b.png"],
        }

    def test_deletes_and_other_buckets_are_ignored(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        routed, _ = _route(
            sessionmaker,
            _connector(),
            [{}],
            [
                _deleted("shots", "a.png"),
                _created("elsewhere", "b.png"),
                _created(None, "c.png"),  # the sender did not say: trusted to the token
            ],
        )
        assert list(routed.values()) == [["c.png"]]

    def test_a_connector_with_no_bucket_takes_any(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        routed, _ = _route(
            sessionmaker, _connector({"root": "/data"}), [{}], [_created("any", "a.png")]
        )
        assert list(routed.values()) == [["a.png"]]

    def test_projects_on_other_connectors_get_nothing(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        routed, _ = _route(
            sessionmaker,
            _connector(),
            [{"source_connector_id": None}],
            [_created("shots", "a.png")],
        )
        assert routed == {}
