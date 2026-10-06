"""Contract tests for the model service.

The output of `/predict` is fed straight into the platform's `AnnotationResult`
model, so these assert on the actual shape of the response rather than on
internal state. If this file passes, the platform can consume this service.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app
from app.schemas import AnnotationResult

SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "object",
            "display_name": "Object",
            "color": "#e11d48",
            "hotkey": "1",
            "tools": ["bbox"],
            "attributes": [],
        }
    ],
    "classification": [],
}

#: A schema with no class that accepts a box.
SCHEMA_NO_BBOX: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "note",
            "display_name": "Note",
            "color": "#0ea5e9",
            "tools": ["classification"],
            "attributes": [],
        }
    ],
    "classification": [],
}


def make_image(width: int = 320, height: int = 240) -> bytes:
    """An image with obvious high-contrast blocks for the detector to find."""
    image = Image.new("RGB", (width, height), (40, 44, 52))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 30, 140, 120], fill=(230, 70, 70))
    draw.rectangle([190, 140, 290, 210], fill=(60, 170, 230))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client() -> Any:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def served_image(httpserver: Any) -> str:
    """Serve a real PNG over HTTP so the backend fetches it as it would live."""
    httpserver.expect_request("/image.png").respond_with_data(
        make_image(), content_type="image/png"
    )
    return str(httpserver.url_for("/image.png"))


class TestHealthAndInfo:
    def test_health(self, client: Any) -> None:
        assert client.get("/health").json() == {"status": "ok"}

    def test_ready_names_the_backend(self, client: Any) -> None:
        body = client.get("/ready").json()
        assert body["status"] == "ok"
        assert body["backend"]

    def test_info_reports_capabilities(self, client: Any) -> None:
        body = client.get("/info").json()
        assert body["task"] == "detect"
        assert "image" in body["media_types"]
        assert isinstance(body["classes"], list)
        assert body["gpu"] is False


class TestPredict:
    def test_empty_batch(self, client: Any) -> None:
        response = client.post("/predict", json={"items": [], "schema": SCHEMA})
        assert response.status_code == 200
        assert response.json()["predictions"] == []

    def test_output_validates_against_the_platform_model(
        self, client: Any, served_image: str
    ) -> None:
        """The contract that matters: the platform can parse what we return."""
        response = client.post(
            "/predict",
            json={
                "items": [{"id": "i1", "url": served_image, "width": 320, "height": 240}],
                "schema": SCHEMA,
            },
        )
        assert response.status_code == 200
        prediction = response.json()["predictions"][0]

        # Raises if the shape does not match the platform's own model.
        result = AnnotationResult.model_validate(prediction["result"])
        assert prediction["item_id"] == "i1"
        assert 0.0 <= prediction["confidence"] <= 1.0
        for shape in result.shapes:
            assert isinstance(shape.id, UUID)
            assert shape.confidence is not None, "model output must carry a confidence (ML-3)"

    def test_boxes_lie_inside_the_image(self, client: Any, served_image: str) -> None:
        response = client.post(
            "/predict",
            json={
                "items": [{"id": "i1", "url": served_image, "width": 320, "height": 240}],
                "schema": SCHEMA,
            },
        )
        result = AnnotationResult.model_validate(response.json()["predictions"][0]["result"])
        for shape in result.shapes:
            x_min, y_min, x_max, y_max = shape.bbox  # type: ignore[union-attr]
            assert x_max > x_min and y_max > y_min
            assert x_min >= 0 and y_min >= 0
            assert x_max <= 320 and y_max <= 240

    def test_deterministic(self, client: Any, served_image: str) -> None:
        """Same image in, identical output — or correction metrics are noise."""
        payload = {
            "items": [{"id": "i1", "url": served_image, "width": 320, "height": 240}],
            "schema": SCHEMA,
        }
        first = client.post("/predict", json=payload).json()["predictions"][0]
        second = client.post("/predict", json=payload).json()["predictions"][0]

        def boxes(prediction: dict[str, Any]) -> list[Any]:
            return [s["bbox"] for s in prediction["result"]["shapes"]]

        assert boxes(first) == boxes(second)

    def test_unreachable_url_yields_an_error_not_a_500(self, client: Any) -> None:
        response = client.post(
            "/predict",
            json={
                "items": [
                    {
                        "id": "i1",
                        "url": "http://127.0.0.1:9/never.png",
                        "width": 320,
                        "height": 240,
                    }
                ],
                "schema": SCHEMA,
            },
        )
        assert response.status_code == 200
        prediction = response.json()["predictions"][0]
        assert prediction["result"]["shapes"] == []
        assert prediction["error"]

    def test_one_bad_item_does_not_fail_the_batch(self, client: Any, served_image: str) -> None:
        response = client.post(
            "/predict",
            json={
                "items": [
                    {"id": "bad", "url": "http://127.0.0.1:9/x.png", "width": 320, "height": 240},
                    {"id": "good", "url": served_image, "width": 320, "height": 240},
                ],
                "schema": SCHEMA,
            },
        )
        assert response.status_code == 200
        predictions = {p["item_id"]: p for p in response.json()["predictions"]}
        assert predictions["bad"]["error"]
        assert predictions["good"]["error"] is None

    def test_refuses_classes_absent_from_the_schema(self, client: Any, served_image: str) -> None:
        """A model must never introduce a class the project has not defined."""
        response = client.post(
            "/predict",
            json={
                "items": [{"id": "i1", "url": served_image, "width": 320, "height": 240}],
                "schema": SCHEMA_NO_BBOX,
            },
        )
        assert response.status_code == 200
        assert response.json()["predictions"][0]["result"]["shapes"] == []

    def test_accepts_every_platform_tool_and_skeletons(
        self, client: Any, served_image: str
    ) -> None:
        """The platform sends its full schema; rbox and keypoint classes must not 422."""
        schema = {
            **SCHEMA,
            "classes": [
                *SCHEMA["classes"],
                {
                    "name": "ship",
                    "display_name": "Ship",
                    "color": "#22c55e",
                    "tools": ["rbox"],
                },
                {
                    "name": "person",
                    "display_name": "Person",
                    "color": "#a855f7",
                    "tools": ["keypoints"],
                    "skeleton": {"points": ["head", "hip"], "edges": [[0, 1]]},
                },
            ],
        }
        response = client.post(
            "/predict",
            json={
                "items": [{"id": "i1", "url": served_image, "width": 320, "height": 240}],
                "schema": schema,
            },
        )
        assert response.status_code == 200
        assert response.json()["predictions"][0]["error"] is None

    def test_confidence_threshold_filters(self, client: Any, served_image: str) -> None:
        item = {"id": "i1", "url": served_image, "width": 320, "height": 240}
        permissive = client.post(
            "/predict", json={"items": [item], "schema": SCHEMA, "confidence_threshold": 0.0}
        ).json()["predictions"][0]
        strict = client.post(
            "/predict", json={"items": [item], "schema": SCHEMA, "confidence_threshold": 1.01}
        ).json()["predictions"][0]

        assert len(strict["result"]["shapes"]) <= len(permissive["result"]["shapes"])
        assert strict["result"]["shapes"] == []

    def test_oversized_batch_is_refused(self, client: Any) -> None:
        items = [
            {"id": str(n), "url": "http://example.invalid/x.png", "width": 10, "height": 10}
            for n in range(300)
        ]
        response = client.post("/predict", json={"items": items, "schema": SCHEMA})
        assert response.status_code == 413


class TestInteractive:
    def test_point_prompt_returns_a_polygon(self, client: Any, served_image: str) -> None:
        response = client.post(
            "/interactive",
            json={
                "item_id": "i1",
                "url": served_image,
                "width": 320,
                "height": 240,
                "point": {"x": 90.0, "y": 75.0},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "polygon"
        assert len(body["points"]) >= 3
        for x, y in body["points"]:
            assert 0 <= x <= 320
            assert 0 <= y <= 240

    def test_a_prompt_is_required(self, client: Any, served_image: str) -> None:
        response = client.post(
            "/interactive",
            json={"item_id": "i1", "url": served_image, "width": 320, "height": 240},
        )
        assert response.status_code == 422


class TestEmbed:
    def test_returns_a_vector_per_item(self, client: Any, served_image: str) -> None:
        response = client.post("/embed", json={"items": [{"id": "i1", "url": served_image}]})
        assert response.status_code == 200
        body = response.json()
        assert body["dimensions"] > 0
        assert len(body["embeddings"][0]["vector"]) == body["dimensions"]

    def test_deterministic(self, client: Any, served_image: str) -> None:
        payload = {"items": [{"id": "i1", "url": served_image}]}
        first = client.post("/embed", json=payload).json()["embeddings"][0]["vector"]
        second = client.post("/embed", json=payload).json()["embeddings"][0]["vector"]
        assert first == second

    def test_empty_batch(self, client: Any) -> None:
        body = client.post("/embed", json={"items": []}).json()
        assert body["embeddings"] == []


class TestApiKey:
    """`MODEL_API_KEY`: optional bearer auth on everything but the probes."""

    PROTECTED = ("/info", "/docs", "/openapi.json")

    def test_unset_leaves_the_service_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODEL_API_KEY", raising=False)
        with TestClient(app) as client:
            assert client.get("/info").status_code == 200

    @pytest.mark.parametrize("path", PROTECTED)
    def test_a_missing_key_is_401(self, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "header", ["Bearer wrong", "Bearer ", "s3cret", "Basic s3cret", "Bearer s3cret2", "Bearer"]
    )
    def test_a_wrong_key_is_401(self, monkeypatch: pytest.MonkeyPatch, header: str) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            assert client.get("/info", headers={"Authorization": header}).status_code == 401

    def test_the_post_routes_are_protected_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            assert client.post("/predict", json={}).status_code == 401
            assert client.post("/embed", json={}).status_code == 401

    def test_the_right_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            ok = client.get("/info", headers={"Authorization": "Bearer s3cret"})
            lower = client.get("/info", headers={"Authorization": "bearer s3cret"})
        assert ok.status_code == 200
        assert lower.status_code == 200

    def test_a_non_ascii_header_is_401_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            response = client.get("/info", headers={b"Authorization": "Bearer é".encode("latin-1")})
        assert response.status_code == 401

    def test_probes_stay_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "s3cret")
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200
