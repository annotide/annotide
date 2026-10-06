"""Tests for app.services.models: class mapping (BYOM-2) and the model client (BYOM-3).

See docs/CONTRACTS.md → "### model / model_version" for the mapping semantics
this exercises: `class_mapping` translates a model's own class names into the
project's schema names, in both directions.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError

from app.schemas import AnnotationResult, LabelSchemaDefinition
from app.services.models import (
    MAX_PREDICT_ITEMS,
    EntraAuth,
    ModelClient,
    ModelRejected,
    ModelUnavailable,
    PredictItem,
    auth_headers,
    entra_auth,
    identity_problem,
    map_result,
    model_facing_schema,
    normalise_mapping,
    validate_mapping,
)

# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #


def _class(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "display_name": name.title(),
        "color": "#e11d48",
        "hotkey": None,
        "tools": ["bbox"],
        "attributes": [],
    }
    base.update(overrides)
    return base


def build_definition(**overrides: Any) -> LabelSchemaDefinition:
    data: dict[str, Any] = {
        "version": 1,
        "classes": [
            _class("car"),
            _class("pedestrian", color="#0ea5e9", tools=["bbox", "polygon"]),
        ],
        "classification": [
            {"name": "weather", "type": "select", "required": True, "options": ["clear", "rain"]}
        ],
    }
    data.update(overrides)
    return LabelSchemaDefinition.model_validate(data)


def _bbox_shape(class_: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "type": "bbox",
        "class": class_,
        "bbox": [1, 2, 30, 40],
        "attributes": {},
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def build_result(shapes: list[dict[str, Any]], **overrides: Any) -> AnnotationResult:
    data: dict[str, Any] = {
        "schema_version": 1,
        "media_type": "image",
        "classification": {"weather": "rain"},
        "shapes": shapes,
    }
    data.update(overrides)
    return AnnotationResult.model_validate(data)


def _items(n: int) -> list[PredictItem]:
    return [
        PredictItem(id=f"item-{i}", url=f"https://example.com/{i}.jpg", width=100, height=100)
        for i in range(n)
    ]


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    headers: dict[str, str] | None = None,
) -> ModelClient:
    """A ModelClient whose transport is a MockTransport instead of the network.

    `ModelClient` only exposes the underlying `httpx.AsyncClient` as a private
    attribute; swapping it after construction is the simplest way to test the
    class without a real HTTP server, so we reach into `_client` directly.
    """
    client = ModelClient("http://model.example", headers or {})
    client._client = httpx.AsyncClient(
        base_url="http://model.example",
        headers=client._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return client


# --------------------------------------------------------------------------- #
# normalise_mapping
# --------------------------------------------------------------------------- #


def test_normalise_mapping_passes_through_a_valid_mapping() -> None:
    raw = {"person": "pedestrian", "vehicle": None}
    assert normalise_mapping(raw) == {"person": "pedestrian", "vehicle": None}


def test_normalise_mapping_rejects_non_string_key() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        normalise_mapping({1: "car"})  # type: ignore[dict-item]


def test_normalise_mapping_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        normalise_mapping({"": "car"})


def test_normalise_mapping_rejects_non_string_value() -> None:
    with pytest.raises(ValueError, match="string or null"):
        normalise_mapping({"person": 1})  # type: ignore[dict-item]


# --------------------------------------------------------------------------- #
# validate_mapping
# --------------------------------------------------------------------------- #


def test_validate_mapping_reports_unknown_targets_sorted() -> None:
    definition = build_definition()
    mapping = {"a": "truck", "b": "bike", "c": "car"}
    assert validate_mapping(mapping, definition) == ["bike", "truck"]


def test_validate_mapping_ignores_null_targets() -> None:
    definition = build_definition()
    mapping = {"a": None, "b": "car"}
    assert validate_mapping(mapping, definition) == []


def test_validate_mapping_empty_mapping_reports_nothing() -> None:
    assert validate_mapping({}, build_definition()) == []


# --------------------------------------------------------------------------- #
# model_facing_schema
# --------------------------------------------------------------------------- #


def test_model_facing_schema_empty_mapping_returns_same_definition() -> None:
    definition = build_definition()
    assert model_facing_schema(definition, {}) is definition


def test_model_facing_schema_renames_mapped_classes_to_model_names() -> None:
    definition = build_definition()
    result = model_facing_schema(definition, {"person": "pedestrian"})
    assert [c.name for c in result.classes] == ["person"]
    mapped = result.classes[0]
    target = next(c for c in definition.classes if c.name == "pedestrian")
    assert mapped.display_name == "person"
    assert mapped.tools == target.tools
    assert mapped.attributes == target.attributes
    assert mapped.color == target.color


def test_model_facing_schema_omits_null_mapped_classes() -> None:
    definition = build_definition()
    result = model_facing_schema(definition, {"person": "car", "background": None})
    assert [c.name for c in result.classes] == ["person"]


def test_model_facing_schema_omits_unknown_targets() -> None:
    definition = build_definition()
    result = model_facing_schema(definition, {"person": "truck"})
    assert result.classes == []


def test_model_facing_schema_preserves_classification() -> None:
    definition = build_definition()
    result = model_facing_schema(definition, {"person": "car"})
    assert result.classification == definition.classification


# --------------------------------------------------------------------------- #
# map_result
# --------------------------------------------------------------------------- #


def test_map_result_renames_mapped_class() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("person")])
    mapped, dropped = map_result(result, {"person": "car"}, definition)
    assert dropped == 0
    assert [s.class_ for s in mapped.shapes] == ["car"]


def test_map_result_drops_and_counts_null_mapped_shapes() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("background"), _bbox_shape("person")])
    mapped, dropped = map_result(result, {"background": None, "person": "car"}, definition)
    assert dropped == 1
    assert [s.class_ for s in mapped.shapes] == ["car"]


def test_map_result_keeps_unmapped_shape_already_in_schema() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("car")])
    mapped, dropped = map_result(result, {}, definition)
    assert dropped == 0
    assert [s.class_ for s in mapped.shapes] == ["car"]


def test_map_result_drops_unmapped_shape_unknown_to_schema() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("truck")])
    mapped, dropped = map_result(result, {}, definition)
    assert dropped == 1
    assert mapped.shapes == []


def test_map_result_leaves_classification_untouched() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("car")], classification={"weather": "snow"})
    mapped, _ = map_result(result, {}, definition)
    assert mapped.classification == {"weather": "snow"}


def test_map_result_does_not_mutate_original_result() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("person")])
    map_result(result, {"person": "car"}, definition)
    assert result.shapes[0].class_ == "person"


def test_map_result_preserves_confidence() -> None:
    definition = build_definition()
    result = build_result([_bbox_shape("person", confidence=0.42)])
    mapped, _ = map_result(result, {"person": "car"}, definition)
    assert mapped.shapes[0].confidence == 0.42


# --------------------------------------------------------------------------- #
# auth_headers
# --------------------------------------------------------------------------- #


async def test_auth_headers_none_returns_empty_and_skips_secret_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(secret_ref: str | None) -> str | None:
        raise AssertionError("resolve_secret must not be called for identity 'none'")

    monkeypatch.setattr("app.services.models.resolve_secret", _boom)
    assert await auth_headers("none", None) == {}


async def test_auth_headers_api_key_uses_x_api_key_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _stub(secret_ref: str | None) -> str | None:
        return "sekret-123"

    monkeypatch.setattr("app.services.models.resolve_secret", _stub)
    assert await auth_headers("api_key", "env:MODEL_KEY") == {"X-API-Key": "sekret-123"}


async def test_auth_headers_bearer_uses_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _stub(secret_ref: str | None) -> str | None:
        return "tok-456"

    monkeypatch.setattr("app.services.models.resolve_secret", _stub)
    headers = await auth_headers("bearer", "env:MODEL_TOKEN")
    assert headers == {"Authorization": "Bearer tok-456"}


async def test_auth_headers_missing_secret_raises_model_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _stub(secret_ref: str | None) -> str | None:
        return None

    monkeypatch.setattr("app.services.models.resolve_secret", _stub)
    with pytest.raises(ModelRejected):
        await auth_headers("api_key", "env:MISSING")


async def test_auth_headers_unknown_identity_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not a valid ModelIdentity"):
        await auth_headers("oauth2", None)


# --------------------------------------------------------------------------- #
# ModelClient
# --------------------------------------------------------------------------- #


async def test_info_returns_payload_and_sends_auth_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["api_key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"name": "demo", "classes": ["car"]})

    client = _mock_client(handler, {"X-API-Key": "abc"})
    data = await client.info()
    assert data == {"name": "demo", "classes": ["car"]}
    assert captured["path"] == "/info"
    assert captured["api_key"] == "abc"


async def test_predict_posts_items_schema_and_threshold() -> None:
    captured: dict[str, Any] = {}
    empty_result = {"schema_version": 1, "media_type": "image", "classification": {}, "shapes": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "predictions": [
                    {"item_id": "item-0", "result": empty_result, "confidence": 0.87},
                    {
                        "item_id": "item-1",
                        "result": empty_result,
                        "confidence": 0.0,
                        "error": "timeout fetching image",
                    },
                ]
            },
        )

    client = _mock_client(handler)
    definition = build_definition()
    predictions = await client.predict(_items(2), definition, confidence_threshold=0.3)

    body = captured["body"]
    assert [i["id"] for i in body["items"]] == ["item-0", "item-1"]
    assert body["schema"] == definition.model_dump(mode="json")
    assert body["confidence_threshold"] == 0.3

    assert len(predictions) == 2
    assert predictions[0].item_id == "item-0"
    assert predictions[0].confidence == 0.87
    assert predictions[0].error is None
    assert predictions[1].error == "timeout fetching image"


async def test_predict_returns_empty_list_for_no_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made for an empty item list")

    client = _mock_client(handler)
    assert await client.predict([], build_definition()) == []


async def test_predict_rejects_more_than_max_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when the batch is too large")

    client = _mock_client(handler)
    with pytest.raises(ValueError, match=str(MAX_PREDICT_ITEMS)):
        await client.predict(_items(MAX_PREDICT_ITEMS + 1), build_definition())


async def test_request_5xx_raises_model_unavailable() -> None:
    client = _mock_client(lambda request: httpx.Response(503, text="down for maintenance"))
    with pytest.raises(ModelUnavailable):
        await client.info()


async def test_request_connect_error_raises_model_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _mock_client(handler)
    with pytest.raises(ModelUnavailable):
        await client.info()


async def test_request_4xx_raises_model_rejected() -> None:
    client = _mock_client(lambda request: httpx.Response(401, text="bad credentials"))
    with pytest.raises(ModelRejected):
        await client.info()


async def test_info_non_json_body_raises_model_rejected() -> None:
    client = _mock_client(lambda request: httpx.Response(200, text="not json at all"))
    with pytest.raises(ModelRejected):
        await client.info()


async def test_predict_missing_predictions_key_raises_model_rejected() -> None:
    client = _mock_client(lambda request: httpx.Response(200, json={"ok": True}))
    with pytest.raises(ModelRejected):
        await client.predict(_items(1), build_definition())


async def test_predict_malformed_entry_raises_model_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": [{"confidence": 0.5}]})

    client = _mock_client(handler)
    with pytest.raises(ModelRejected):
        await client.predict(_items(1), build_definition())


async def test_for_model_and_context_manager_closes_client() -> None:
    client = await ModelClient.for_model("http://model.example", "none", None)
    async with client as ctx:
        assert ctx is client
    assert client._client.is_closed


def test_map_result_drops_shape_whose_target_class_lacks_the_tool() -> None:
    """A bbox mapped onto a polygon-only class is dropped, not stored unvalidated."""
    definition = LabelSchemaDefinition.model_validate(
        {
            "version": 1,
            "classes": [
                {
                    "name": "road",
                    "display_name": "Road",
                    "color": "#000000",
                    "tools": ["polygon"],
                    "attributes": [],
                }
            ],
            "classification": [],
        }
    )
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "classification": {},
            "shapes": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "type": "bbox",
                    "class": "surface",
                    "bbox": [1, 2, 3, 4],
                    "attributes": {},
                    "confidence": 0.5,
                }
            ],
        }
    )
    mapped, dropped = map_result(result, {"surface": "road"}, definition)
    assert mapped.shapes == []
    assert dropped == 1


# --------------------------------------------------------------------------- #
# ModelClient.interactive (ML-7)
# --------------------------------------------------------------------------- #


async def test_interactive_posts_point_prompt_and_clamps_points() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/interactive"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "type": "polygon",
                "points": [[-5, 10], [150, 10], [50, 120]],
                "confidence": 0.9,
            },
        )

    async with _mock_client(handler) as client:
        polygon = await client.interactive(_items(1)[0], point=(30.0, 20.0))

    assert seen == [
        {
            "item_id": "item-0",
            "url": "https://example.com/0.jpg",
            "width": 100,
            "height": 100,
            "point": {"x": 30.0, "y": 20.0},
        }
    ]
    assert polygon.points == [(0.0, 10.0), (100.0, 10.0), (50.0, 100.0)]
    assert polygon.confidence == 0.9


async def test_interactive_posts_box_prompt() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"points": [[1, 1], [2, 1], [2, 2]], "confidence": 0.1})

    async with _mock_client(handler) as client:
        await client.interactive(_items(1)[0], box=(1.0, 2.0, 30.0, 40.0))

    assert seen[0]["box"] == {"bbox": [1.0, 2.0, 30.0, 40.0]}
    assert "point" not in seen[0]


@pytest.mark.parametrize(
    "body",
    [
        {"points": [[1, 1], [2, 2]]},
        {"points": "nope"},
        {"points": [[1, 1], [2], [3, 3]]},
        {"points": [[1, 1], ["x", 2], [3, 3]]},
        [],
    ],
)
async def test_interactive_rejects_malformed_polygon(body: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _mock_client(handler) as client:
        with pytest.raises(ModelRejected):
            await client.interactive(_items(1)[0], point=(1.0, 1.0))


async def test_interactive_server_error_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _mock_client(handler) as client:
        with pytest.raises(ModelUnavailable):
            await client.interactive(_items(1)[0], point=(1.0, 1.0))


# --------------------------------------------------------------------------- #
# ModelClient.ocr
# --------------------------------------------------------------------------- #


def _pdf_item() -> PredictItem:
    return PredictItem(
        id="doc", url="https://example.com/doc.pdf", width=0, height=0, media_type="pdf"
    )


async def test_ocr_posts_one_page_and_sanitises_words() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ocr"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "engine": "tesseract",
                "pages": [
                    {
                        "page": 2,
                        "width": 600,
                        "height": 800,
                        "words": [
                            {"text": " Total\n", "bbox": [100, 90, 140, 100], "confidence": 0.9},
                            {"text": "42,00", "bbox": [590, 90, 650, 100]},
                            {"text": "flat", "bbox": [10, 10, 10, 20]},
                            {"text": "", "bbox": [1, 1, 2, 2]},
                            {"text": "short", "bbox": [1, 2]},
                            {"bbox": [1, 1, 2, 2]},
                            "junk",
                        ],
                    }
                ],
            },
        )

    async with _mock_client(handler) as client:
        page = await client.ocr(_pdf_item(), 2)

    assert seen == [
        {"item_id": "doc", "url": "https://example.com/doc.pdf", "media_type": "pdf", "pages": [2]}
    ]
    assert (page.page, page.width, page.height, page.engine) == (2, 600.0, 800.0, "tesseract")
    assert page.words == [
        ("Total", (100.0, 90.0, 140.0, 100.0)),
        ("42,00", (590.0, 90.0, 600.0, 100.0)),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"pages": []},
        {"pages": [{"page": 3, "width": 600, "height": 800, "words": []}]},
        {"pages": [{"page": 2, "width": 0, "height": 800, "words": []}]},
        {"pages": [{"page": 2, "width": 600, "height": 800, "words": "none"}]},
        {"pages": [{"page": 2, "width": "wide", "height": 800, "words": []}]},
        {"pages": ["page"]},
        [],
    ],
)
async def test_ocr_rejects_malformed_pages(body: Any) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _mock_client(handler) as client:
        with pytest.raises(ModelRejected):
            await client.ocr(_pdf_item(), 2)


async def test_ocr_off_or_failing_is_unavailable_and_a_bad_request_rejected() -> None:
    # 501: the service has no OCR engine; 502: the engine failed; 400: bad pdf.
    answers = iter([httpx.Response(501), httpx.Response(502), httpx.Response(400)])

    def handler(_: httpx.Request) -> httpx.Response:
        return next(answers)

    async with _mock_client(handler) as client:
        with pytest.raises(ModelUnavailable, match="501"):
            await client.ocr(_pdf_item(), 1)
        with pytest.raises(ModelUnavailable, match="502"):
            await client.ocr(_pdf_item(), 1)
        with pytest.raises(ModelRejected, match="400"):
            await client.ocr(_pdf_item(), 1)


# --------------------------------------------------------------------------- #
# Entra identities (BYOM-3): managed identity, service principal
# --------------------------------------------------------------------------- #

SCOPE = "https://ml.azure.com/.default"
SP_CONFIG = {"scope": SCOPE, "tenant_id": "tenant", "client_id": "app"}


@pytest.mark.parametrize(
    ("identity", "secret_ref", "config", "problem"),
    [
        ("none", None, {}, None),
        ("bearer", "env:T", {}, None),
        ("api_key", None, {}, "requires a secret_ref"),
        ("managed_identity", None, {"scope": SCOPE}, None),
        ("managed_identity", None, {"scope": SCOPE, "client_id": "uami"}, None),
        ("managed_identity", None, {}, "identity_config.scope"),
        ("managed_identity", "env:X", {"scope": SCOPE}, "takes no secret_ref"),
        ("service_principal", "env:S", SP_CONFIG, None),
        ("service_principal", None, SP_CONFIG, "requires a secret_ref"),
        ("service_principal", "env:S", {"scope": SCOPE}, "identity_config.tenant_id, client_id"),
    ],
)
def test_identity_problem(
    identity: str, secret_ref: str | None, config: dict[str, str], problem: str | None
) -> None:
    found = identity_problem(identity, secret_ref, config)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


async def test_auth_headers_refuses_entra_identities() -> None:
    with pytest.raises(ValueError, match="per request"):
        await auth_headers("managed_identity", None)


class _Credential:
    """Counts token requests; each gets a new token. `error` makes them fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scopes: list[str] = []
        self.closed = False

    async def get_token(self, *scopes: str, **_: Any) -> AccessToken:
        if self.error is not None:
            raise self.error
        self.scopes.append(scopes[0])
        return AccessToken(f"token-{len(self.scopes)}", 2_000_000_000)

    async def close(self) -> None:
        self.closed = True


def _entra_client(credential: _Credential, seen: list[str | None]) -> ModelClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"name": "m", "classes": []})

    auth = EntraAuth(credential, SCOPE)  # type: ignore[arg-type]  # duck-typed credential
    client = ModelClient("http://model.example", {}, auth)
    client._client = httpx.AsyncClient(
        base_url="http://model.example", auth=auth, transport=httpx.MockTransport(handler)
    )
    return client


async def test_entra_auth_gets_a_token_for_every_request() -> None:
    """A long prelabel job keeps one client; each call must carry a current token."""
    credential = _Credential()
    seen: list[str | None] = []
    async with _entra_client(credential, seen) as client:
        await client.info()
        await client.info()

    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert credential.scopes == [SCOPE, SCOPE]
    assert credential.closed  # closed with the client


async def test_entra_sign_in_failure_is_permanent() -> None:
    credential = _Credential(ClientAuthenticationError("AADSTS7000215: invalid secret"))
    async with _entra_client(credential, []) as client:
        with pytest.raises(ModelRejected, match="AADSTS7000215"):
            await client.info()


async def test_unreachable_entra_is_transient() -> None:
    credential = _Credential(ServiceRequestError("connection reset"))
    async with _entra_client(credential, []) as client:
        with pytest.raises(ModelUnavailable, match="ServiceRequestError"):
            await client.info()


async def test_managed_identity_uses_the_user_assigned_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, Any] = {}

    class _MI(_Credential):
        def __init__(self, client_id: str | None = None) -> None:
            super().__init__()
            built["client_id"] = client_id

    monkeypatch.setattr("azure.identity.aio.ManagedIdentityCredential", _MI)

    auth = await entra_auth("managed_identity", None, {"scope": SCOPE, "client_id": "uami"})
    assert built == {"client_id": "uami"}
    await entra_auth("managed_identity", None, {"scope": SCOPE})
    assert built == {"client_id": None}  # system-assigned / workload identity
    await auth.aclose()


async def test_service_principal_resolves_its_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, Any] = {}

    class _SP(_Credential):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__()
            built.update(kwargs)

    async def _secret(ref: str | None) -> str | None:
        return "client-secret" if ref == "env:SP" else None

    monkeypatch.setattr("azure.identity.aio.ClientSecretCredential", _SP)
    monkeypatch.setattr("app.services.models.resolve_secret", _secret)

    await entra_auth("service_principal", "env:SP", SP_CONFIG)
    assert built == {"tenant_id": "tenant", "client_id": "app", "client_secret": "client-secret"}

    with pytest.raises(ModelRejected, match="with a value"):
        await entra_auth("service_principal", "env:EMPTY", SP_CONFIG)


async def test_for_model_with_an_incomplete_entra_identity_is_rejected() -> None:
    with pytest.raises(ModelRejected, match=r"identity_config\.scope"):
        await ModelClient.for_model("http://model.example", "managed_identity", None, {})
