"""The sam2 backend against a fake onnxruntime (the real one and the weights are extras)."""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.backends.sam2 import DECODER_FILE, ENCODER_FILE, NO_OBJECT, Sam2Backend, preprocess
from app.schemas import BoxPrompt, InteractiveRequest, PointPrompt

WIDTH, HEIGHT = 800, 400
#: The fake decoder's object, in its 256 x 256 mask: columns 64-127, rows 128-191,
#: which is x 200-400, y 200-300 of the 800 x 400 image.
OBJECT = (slice(128, 192), slice(64, 128))


class _Encoder:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, names: list[str], feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls += 1
        assert feeds["pixel_values"].shape == (1, 3, 1024, 1024)
        shapes = [(1, 32, 256, 256), (1, 64, 128, 128), (1, 256, 64, 64)]
        return [np.zeros(shape, dtype=np.float32) for shape in shapes[: len(names)]]


class _Decoder:
    def __init__(self, inputs: list[str]) -> None:
        self._inputs = inputs
        self.feeds: list[dict[str, np.ndarray]] = []
        self.objectness = 5.0

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name=name) for name in self._inputs]

    def run(self, _: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.feeds.append(feeds)
        masks = np.full((1, 1, 3, 256, 256), -10.0, dtype=np.float32)
        masks[0, 0, 1][OBJECT] = 10.0  # the best-rated mask: the object
        masks[0, 0, 0][:, :] = 10.0  # mask 0: everything
        iou = np.array([[[0.5, 0.9, 0.2]]], dtype=np.float32)
        return [iou, masks, np.array([[[self.objectness]]], dtype=np.float32)]


DECODER_INPUTS = [
    "input_points",
    "input_labels",
    "input_boxes",
    "image_embeddings.0",
    "image_embeddings.1",
    "image_embeddings.2",
]


def _runtime(monkeypatch: pytest.MonkeyPatch, decoder_inputs: list[str] = DECODER_INPUTS) -> None:
    def session(path: str, providers: list[str]) -> object:
        if path.endswith(ENCODER_FILE):
            return _Encoder()
        return _Decoder(decoder_inputs)

    runtime = SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"], InferenceSession=session
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)


def _weights(tmp_path: Path) -> Path:
    directory = tmp_path / "sam2.1-hiera-tiny"
    directory.mkdir()
    for name in (ENCODER_FILE, DECODER_FILE):
        (directory / name).write_bytes(b"onnx")
    return directory


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Sam2Backend:
    _runtime(monkeypatch)
    return Sam2Backend(str(_weights(tmp_path)), cache_items=2)


def _url() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (WIDTH, HEIGHT), (10, 20, 30)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _request(item: str = "item-1", **prompt: Any) -> InteractiveRequest:
    return InteractiveRequest(item_id=item, url=_url(), width=WIDTH, height=HEIGHT, **prompt)


def _bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


class TestStartup:
    def test_needs_a_directory_with_both_models(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _runtime(monkeypatch)
        monkeypatch.delenv("MODEL_PATH", raising=False)
        with pytest.raises(ValueError, match="requires MODEL_PATH"):
            Sam2Backend()
        with pytest.raises(ValueError, match=f"no {ENCODER_FILE}"):
            Sam2Backend(str(tmp_path))

    def test_refuses_a_decoder_that_is_not_sam2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _runtime(monkeypatch, decoder_inputs=["images"])
        with pytest.raises(ValueError, match="not a SAM2 decoder"):
            Sam2Backend(str(_weights(tmp_path)))

    def test_identity(self, backend: Sam2Backend) -> None:
        assert (backend.name, backend.version, backend.gpu) == ("sam2", "sam2.1-hiera-tiny", False)
        assert backend.classes() == []
        assert backend.task == "segment"


class TestInteractive:
    async def test_a_click_outlines_the_best_rated_mask(self, backend: Sam2Backend) -> None:
        answer = await backend.interactive(_request(point=PointPrompt(x=300, y=250)))
        x_min, y_min, x_max, y_max = _bounds(answer.points)
        assert (x_min, y_min) == pytest.approx((200, 200), abs=2)
        assert (x_max, y_max) == pytest.approx((400, 300), abs=2)
        assert answer.confidence == pytest.approx(0.9)
        # The click went to the decoder in the encoder's 1024 x 1024 space.
        feeds = backend._decoder.feeds[0]
        assert feeds["input_points"].tolist() == [[[[384.0, 640.0]]]]
        assert feeds["input_labels"].tolist() == [[[1]]]
        assert feeds["input_boxes"].shape == (1, 0, 4)

    async def test_a_box_takes_the_first_mask(self, backend: Sam2Backend) -> None:
        answer = await backend.interactive(_request(box=BoxPrompt(bbox=(100, 100, 500, 300))))
        assert _bounds(answer.points) == pytest.approx((0, 0, WIDTH, HEIGHT), abs=2)
        assert answer.confidence == pytest.approx(0.5)
        feeds = backend._decoder.feeds[0]
        assert feeds["input_boxes"].tolist() == [[[128.0, 256.0, 640.0, 768.0]]]
        assert feeds["input_points"].shape == (1, 1, 0, 2)

    async def test_no_object_is_an_error_not_an_empty_polygon(self, backend: Sam2Backend) -> None:
        backend._decoder.objectness = -3.0
        with pytest.raises(ValueError, match=NO_OBJECT):
            await backend.interactive(_request(point=PointPrompt(x=300, y=250)))

    async def test_text_prompts_are_refused(self, backend: Sam2Backend) -> None:
        with pytest.raises(ValueError, match="not text"):
            await backend.interactive(_request(text="a car"))

    async def test_an_unreadable_item_is_an_error(self, backend: Sam2Backend) -> None:
        request = _request(point=PointPrompt(x=1, y=1))
        request.url = "data:image/png;base64," + base64.b64encode(b"not a png").decode()
        with pytest.raises(ValueError, match="could not load the item"):
            await backend.interactive(request)


class TestEncoderCache:
    async def test_each_item_is_encoded_once(self, backend: Sam2Backend) -> None:
        click = PointPrompt(x=300, y=250)
        await backend.interactive(_request("a", point=click))
        await backend.interactive(_request("a", point=click))
        assert backend._encoder.calls == 1

    async def test_concurrent_clicks_share_one_encoding(self, backend: Sam2Backend) -> None:
        click = PointPrompt(x=300, y=250)
        await asyncio.gather(*(backend.interactive(_request("a", point=click)) for _ in range(3)))
        assert backend._encoder.calls == 1

    async def test_the_least_recently_used_item_is_dropped(self, backend: Sam2Backend) -> None:
        click = PointPrompt(x=300, y=250)
        for item in ("a", "b", "a", "c", "a", "b"):
            await backend.interactive(_request(item, point=click))
        # Capacity 2: a, b, (a hit), c evicts b, (a hit), b again.
        assert backend._encoder.calls == 4
        assert list(backend._cache) == ["a", "b"]


class TestUnsupported:
    async def test_no_prelabelling_or_embeddings(self, backend: Sam2Backend) -> None:
        with pytest.raises(ValueError, match="does not pre-label"):
            await backend.predict([], schema=None, confidence_threshold=0.5)  # type: ignore[arg-type]  # never read
        with pytest.raises(ValueError, match="embeddings"):
            await backend.embed([])


def test_preprocess_normalises_like_the_sam2_processor() -> None:
    pixels = preprocess(Image.new("RGB", (10, 20), (255, 0, 128)))
    assert pixels.shape == (1, 3, 1024, 1024)
    assert pixels.dtype == np.float32
    expected = (np.array([1.0, 0.0, 128 / 255]) - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    assert pixels[0, :, 500, 500] == pytest.approx(expected, abs=1e-5)
