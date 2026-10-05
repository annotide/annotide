"""OCR: the engines, the page pipeline, `/ocr` and the pre-labelling fallback."""

from __future__ import annotations

import json
import shutil
from typing import Any

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from werkzeug import Request, Response

from app import ocr
from app.main import app
from app.ocr import OcrError, OcrWord
from app.ocr.llm import (
    SCHEMA,
    AnthropicEngine,
    OpenAICompatibleEngine,
    extract_json,
    parse_words,
)
from app.ocr.tesseract import TesseractEngine, parse_tsv
from app.schemas import AnnotationResult
from tests.pdfs import pdf, scan

FIELDS_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {
            "name": "amount",
            "display_name": "Amount",
            "color": "#e11d48",
            "tools": ["bbox"],
            "attributes": [],
        }
    ],
    "classification": [],
}


class FakeEngine:
    """Answers one fixed word per page, in the pixels of the image it got."""

    name = "fake"
    dpi = 144
    max_side = 4000

    def __init__(self, text: str = "42,00") -> None:
        self.text = text
        self.sizes: list[tuple[int, int]] = []

    async def read(self, image: Image.Image) -> list[OcrWord]:
        self.sizes.append(image.size)
        # 100..180 x 90..100 pt at 2 px per pt (144 dpi), plus a word off the page.
        return [
            OcrWord(self.text, (200.0, 180.0, 360.0, 200.0), 0.9),
            OcrWord("  ", (0.0, 0.0, 10.0, 10.0)),
            OcrWord("edge", (1190.0, 1590.0, 1300.0, 1700.0)),
        ]


def use_engine(monkeypatch: pytest.MonkeyPatch, engine: Any) -> None:
    monkeypatch.setattr(ocr, "get_engine", lambda name=None: engine)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestParsing:
    def test_tesseract_tsv_keeps_confident_words(self) -> None:
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "1\t1\t0\t0\t0\t0\t0\t0\t2500\t3333\t-1\t\n"
            "5\t1\t1\t1\t1\t1\t417\t375\t300\t50\t96.5\tInvoice\n"
            "5\t1\t1\t1\t1\t2\t730\t375\t80\t50\t12\t~\n"
            "5\t1\t1\t1\t1\t3\t820\t375\t80\t50\t91\t \n"
        )
        assert parse_tsv(tsv, 0.3) == [OcrWord("Invoice", (417.0, 375.0, 717.0, 425.0), 0.965)]
        assert parse_tsv("") == []
        with pytest.raises(OcrError):
            parse_tsv("not\ttsv\n")

    def test_llm_words_are_scaled_from_the_grid_and_sanitised(self) -> None:
        payload = {
            "words": [
                {"text": " Total ", "x0": 100, "y0": 500, "x1": 300, "y1": 550},
                {"text": "42", "box": [400, 500, 500, 550]},
                {"text": "wild", "x0": -20, "y0": 900, "x1": 2000, "y1": 950},
                {"text": "flat", "x0": 10, "y0": 10, "x1": 10, "y1": 20},
                {"text": "bad", "x0": "1", "y0": 0, "x1": 2, "y1": 3},
                {"text": "bool", "x0": True, "y0": 0, "x1": 2, "y1": 3},
                "junk",
            ]
        }
        words = parse_words(payload, 2000, 1000)
        assert [(w.text, w.bbox) for w in words] == [
            ("Total", (200.0, 500.0, 600.0, 550.0)),
            ("42", (800.0, 500.0, 1000.0, 550.0)),
            ("wild", (0.0, 900.0, 2000.0, 950.0)),
        ]
        with pytest.raises(OcrError):
            parse_words({"lines": []}, 10, 10)

    def test_json_is_found_in_fences_and_preambles(self) -> None:
        assert extract_json('```json\n{"words": []}\n```') == {"words": []}
        assert extract_json('Here you go: {"words": []} Hope it helps') == {"words": []}
        with pytest.raises(OcrError):
            extract_json("I cannot read this page.")
        with pytest.raises(OcrError):
            extract_json("{not json}")


# --------------------------------------------------------------------------- #
# Engine selection
# --------------------------------------------------------------------------- #


class TestEngineSelection:
    def test_none_unknown_and_missing_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ocr.get_engine("none") is None
        with pytest.raises(ValueError, match="unknown OCR_ENGINE"):
            ocr.get_engine("abbyy")
        monkeypatch.delenv("OCR_LLM_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="OCR_LLM_BASE_URL"):
            ocr.get_engine("openai")

    def test_configured_engines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCR_LLM_BASE_URL", "http://llm.internal/v1/")
        monkeypatch.setenv("OCR_LLM_MODEL", "qwen2.5-vl")
        engine = ocr.get_engine("openai")
        assert isinstance(engine, OpenAICompatibleEngine)
        assert engine.url == "http://llm.internal/v1/chat/completions"

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.delenv("OCR_LLM_MODEL")
        claude = ocr.get_engine("anthropic")
        assert isinstance(claude, AnthropicEngine)
        assert claude.model == "claude-opus-5"
        assert claude.effort == "medium"

        monkeypatch.setenv("OCR_LANGUAGES", "fin+eng")
        tesseract = ocr.get_engine("tesseract")
        assert isinstance(tesseract, TesseractEngine)
        assert tesseract.languages == "fin+eng"

    def test_auto_follows_the_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TESSERACT_BIN", "no-such-tesseract")
        assert ocr.get_engine("auto") is None


# --------------------------------------------------------------------------- #
# The page pipeline and /ocr
# --------------------------------------------------------------------------- #


class TestOcrEndpoint:
    def test_pdf_words_come_back_in_page_points(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        engine = FakeEngine()
        use_engine(monkeypatch, engine)
        httpserver.expect_request("/scan.pdf").respond_with_data(scan(["", ""]))
        with TestClient(app) as client:
            assert client.get("/info").json()["ocr_engine"] == "fake"
            response = client.post(
                "/ocr",
                json={"item_id": "doc", "url": httpserver.url_for("/scan.pdf"), "pages": [2]},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["engine"] == "fake"
        [page] = body["pages"]
        assert (page["page"], page["width"], page["height"]) == (2, 600.0, 800.0)
        # 144 dpi: 2 px per point; the empty word is dropped, the edge one clamped.
        assert engine.sizes == [(1200, 1600)]
        assert page["words"] == [
            {"text": "42,00", "bbox": [100.0, 90.0, 180.0, 100.0], "confidence": 0.9},
            {"text": "edge", "bbox": [595.0, 795.0, 600.0, 800.0], "confidence": None},
        ]

    def test_all_pages_up_to_the_limit_and_errors(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        use_engine(monkeypatch, FakeEngine())
        monkeypatch.setenv("OCR_MAX_PAGES", "2")
        httpserver.expect_request("/scan.pdf").respond_with_data(scan(["", "", ""], dpi=72))
        httpserver.expect_request("/bad.pdf").respond_with_data(b"nope")
        url = httpserver.url_for("/scan.pdf")
        with TestClient(app) as client:
            pages = client.post("/ocr", json={"item_id": "d", "url": url}).json()["pages"]
            assert [p["page"] for p in pages] == [1, 2]
            too_many = client.post("/ocr", json={"item_id": "d", "url": url, "pages": [1, 2, 3]})
            assert too_many.status_code == 413
            outside = client.post("/ocr", json={"item_id": "d", "url": url, "pages": [9]})
            assert outside.status_code == 400
            assert "outside 1..3" in outside.json()["detail"]
            bad = httpserver.url_for("/bad.pdf")
            assert client.post("/ocr", json={"item_id": "d", "url": bad}).status_code == 400
            text = client.post("/ocr", json={"item_id": "d", "url": url, "media_type": "text"})
            assert text.status_code == 422

    def test_an_image_is_read_in_its_pixels(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        import io

        engine = FakeEngine()
        engine.max_side = 600
        use_engine(monkeypatch, engine)
        buffer = io.BytesIO()
        Image.new("RGB", (1200, 800), "white").save(buffer, format="PNG")
        httpserver.expect_request("/page.png").respond_with_data(buffer.getvalue())
        with TestClient(app) as client:
            body = client.post(
                "/ocr",
                json={
                    "item_id": "i",
                    "url": httpserver.url_for("/page.png"),
                    "media_type": "image",
                },
            ).json()
        # Downscaled to 600 x 400 for the engine, answered back in 1200 x 800 pixels.
        assert engine.sizes == [(600, 400)]
        assert body["pages"][0]["words"][0]["bbox"] == [400.0, 360.0, 720.0, 400.0]

    def test_off_is_501_and_engine_failures_are_502(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        httpserver.expect_request("/scan.pdf").respond_with_data(scan([""], dpi=72))
        url = httpserver.url_for("/scan.pdf")
        with TestClient(app) as client:
            off = client.post("/ocr", json={"item_id": "d", "url": url})
            assert off.status_code == 501
            assert client.get("/info").json()["ocr_engine"] is None

        class Broken(FakeEngine):
            async def read(self, image: Image.Image) -> list[OcrWord]:
                raise OcrError("the model declined to read this page")

        use_engine(monkeypatch, Broken())
        with TestClient(app) as client:
            failed = client.post("/ocr", json={"item_id": "d", "url": url})
        assert failed.status_code == 502
        assert failed.json()["detail"] == "the model declined to read this page"


class TestPrelabelScans:
    def _predict(self, client: TestClient, url: str) -> dict[str, Any]:
        response = client.post(
            "/predict",
            json={
                "items": [{"id": "doc", "url": url, "width": 0, "height": 0, "media_type": "pdf"}],
                "schema": FIELDS_SCHEMA,
            },
        )
        assert response.status_code == 200, response.text
        [prediction] = response.json()["predictions"]
        return prediction  # type: ignore[no-any-return]

    def test_scanned_pages_are_read_before_the_field_rules(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        use_engine(monkeypatch, FakeEngine())
        # Page 1 is born-digital (no amount), page 2 a scan.
        document = pdf(["Cover", ""])
        httpserver.expect_request("/doc.pdf").respond_with_data(document)
        with TestClient(app) as client:
            prediction = self._predict(client, httpserver.url_for("/doc.pdf"))
        result = AnnotationResult.model_validate(prediction["result"])
        [shape] = result.shapes
        assert (shape.class_, shape.page, shape.text) == ("amount", 2, "42,00")  # type: ignore[union-attr]
        assert shape.bbox == (100.0, 90.0, 180.0, 100.0)  # type: ignore[union-attr]

    def test_an_ocr_failure_is_an_item_error(
        self, monkeypatch: pytest.MonkeyPatch, httpserver: Any
    ) -> None:
        class Broken(FakeEngine):
            async def read(self, image: Image.Image) -> list[OcrWord]:
                raise OcrError("tesseract failed: no language data")

        use_engine(monkeypatch, Broken())
        httpserver.expect_request("/doc.pdf").respond_with_data(pdf([""]))
        with TestClient(app) as client:
            prediction = self._predict(client, httpserver.url_for("/doc.pdf"))
        assert prediction["error"] == "ocr failed: tesseract failed: no language data"
        assert prediction["result"]["shapes"] == []


# --------------------------------------------------------------------------- #
# Tesseract, for real when installed
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
class TestTesseract:
    async def test_reads_a_scanned_page(self) -> None:
        [page] = await ocr.ocr_pdf(scan(["Invoice total 42,00 EUR"]), [1], TesseractEngine())
        words = {w.text: w for w in page.words}
        assert {"Invoice", "total", "42,00", "EUR"} <= set(words)
        # The text sits at x = 100 pt, its baseline 100 pt from the top (12 pt type).
        x0, y0, x1, y1 = words["Invoice"].bbox
        assert 97 <= x0 <= 103 and 88 <= y0 <= 94 and 99 <= y1 <= 104 and x1 > x0
        assert (words["Invoice"].confidence or 0) > 0.5

    async def test_a_missing_binary_or_language_is_an_ocr_error(self) -> None:
        image = Image.new("RGB", (100, 50), "white")
        with pytest.raises(OcrError, match="cannot run"):
            await TesseractEngine(binary="no-such-tesseract").read(image)
        with pytest.raises(OcrError, match="tesseract failed"):
            await TesseractEngine(languages="xx-none").read(image)


# --------------------------------------------------------------------------- #
# LLM engines against fake endpoints
# --------------------------------------------------------------------------- #


def _sse(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def _claude_stream(text: str, stop_reason: str = "end_turn") -> str:
    message: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
    return _sse(
        [
            ("message_start", {"type": "message_start", "message": message}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 20},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


class TestAnthropicEngine:
    def _engine(self, httpserver: Any, **kwargs: Any) -> AnthropicEngine:
        client = anthropic.AsyncAnthropic(
            api_key="test-key", base_url=httpserver.url_for("/"), max_retries=0
        )
        return AnthropicEngine(client=client, **kwargs)

    async def test_claude_reads_the_page_with_structured_output(self, httpserver: Any) -> None:
        seen: list[Request] = []

        def handler(request: Request) -> Response:
            seen.append(request)
            answer = json.dumps(
                {"words": [{"text": "Total", "x0": 100, "y0": 200, "x1": 300, "y1": 250}]}
            )
            return Response(_claude_stream(answer), content_type="text/event-stream")

        httpserver.expect_request("/v1/messages", method="POST").respond_with_handler(handler)
        words = await self._engine(httpserver).read(Image.new("RGB", (1000, 2000), "white"))
        assert words == [OcrWord("Total", (100.0, 400.0, 300.0, 500.0))]

        [request] = seen
        body = request.get_json()
        assert body["model"] == "claude-opus-5"
        assert body["stream"] is True
        assert body["output_config"] == {
            "format": {"type": "json_schema", "schema": SCHEMA},
            "effort": "medium",
        }
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
        image, prompt = body["messages"][0]["content"]
        assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
        assert prompt["type"] == "text" and "0-1000 grid" in prompt["text"]
        assert request.headers["x-api-key"] == "test-key"

    async def test_options_off_refusals_and_errors(self, httpserver: Any) -> None:
        bodies: list[dict[str, Any]] = []

        def refuse(request: Request) -> Response:
            bodies.append(request.get_json())
            return Response(_claude_stream("", "refusal"), content_type="text/event-stream")

        httpserver.expect_ordered_request("/v1/messages").respond_with_handler(refuse)
        httpserver.expect_ordered_request("/v1/messages").respond_with_json(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            status=529,
        )
        engine = self._engine(httpserver, effort=None, fallbacks=False, model="claude-haiku-4-5")
        image = Image.new("RGB", (100, 100), "white")
        with pytest.raises(OcrError, match="declined"):
            await engine.read(image)
        assert "fallbacks" not in bodies[0] and "effort" not in bodies[0]["output_config"]
        assert bodies[0]["model"] == "claude-haiku-4-5"
        with pytest.raises(OcrError, match="529"):
            await engine.read(image)


class TestOpenAICompatibleEngine:
    def _completion(self, content: Any, finish: str = "stop") -> dict[str, Any]:
        return {
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
            ]
        }

    async def test_posts_a_vision_chat_completion(self, httpserver: Any) -> None:
        seen: list[Request] = []

        def handler(request: Request) -> Response:
            seen.append(request)
            answer = '```json\n{"words": [{"text": "42", "box": [500, 500, 600, 600]}]}\n```'
            return Response(json.dumps(self._completion(answer)), content_type="application/json")

        httpserver.expect_request("/v1/chat/completions", method="POST").respond_with_handler(
            handler
        )
        engine = OpenAICompatibleEngine(
            base_url=httpserver.url_for("/v1"), model="qwen2.5-vl", api_key="secret"
        )
        words = await engine.read(Image.new("RGB", (200, 100), "white"))
        assert words == [OcrWord("42", (100.0, 50.0, 120.0, 60.0))]
        [request] = seen
        assert request.headers["Authorization"] == "Bearer secret"
        body = request.get_json()
        assert body["model"] == "qwen2.5-vl"
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        text, image = body["messages"][0]["content"]
        assert text["type"] == "text"
        assert image["image_url"]["url"].startswith("data:image/png;base64,")

    async def test_azure_key_header_and_failures(self, httpserver: Any) -> None:
        seen: list[Request] = []

        def parts(request: Request) -> Response:
            seen.append(request)
            content = [{"type": "text", "text": '{"words": []}'}]
            return Response(json.dumps(self._completion(content)), content_type="application/json")

        route = "/openai/v1/chat/completions"
        httpserver.expect_ordered_request(route).respond_with_handler(parts)
        httpserver.expect_ordered_request(route).respond_with_json(self._completion("{", "length"))
        httpserver.expect_ordered_request(route).respond_with_json({"oops": True})
        httpserver.expect_ordered_request(route).respond_with_data("nope", status=401)
        engine = OpenAICompatibleEngine(
            base_url=httpserver.url_for("/openai/v1"),
            model="gpt-vision",
            api_key="azure-key",
            api_key_header="api-key",
            response_format="json_object",
        )
        image = Image.new("RGB", (10, 10), "white")
        assert await engine.read(image) == []
        assert seen[0].headers["api-key"] == "azure-key"
        assert seen[0].get_json()["response_format"] == {"type": "json_object"}
        with pytest.raises(OcrError, match="more text"):
            await engine.read(image)
        with pytest.raises(OcrError, match="not a chat completion"):
            await engine.read(image)
        with pytest.raises(OcrError, match="401"):
            await engine.read(image)

    async def test_unreachable_and_bad_settings(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        engine = OpenAICompatibleEngine(
            base_url="http://llm.invalid/v1",
            model="m",
            response_format="none",
            transport=httpx.MockTransport(refuse),
        )
        with pytest.raises(OcrError, match="cannot reach"):
            await engine.read(Image.new("RGB", (10, 10), "white"))
        with pytest.raises(ValueError, match="RESPONSE_FORMAT"):
            OpenAICompatibleEngine(base_url="http://x", model="m", response_format="xml")
