"""SAM2 interactive segmentation on ONNX Runtime (ML-7).

A click or a box becomes a polygon around the object under it. SAM2 splits
into an image encoder (heavy: ~1 s on a CPU for the tiny model) and a
prompt decoder (light: tens of milliseconds). The encoder runs once per item
and its output is kept in memory, so every further click on the same image
runs only the decoder. That is what makes the tiny and small models usable
on a CPU in the annotator's inner loop.

`MODEL_PATH` is a directory holding `vision_encoder.onnx` and
`prompt_encoder_mask_decoder.onnx` (with their `.onnx_data` files), the
layout of the `onnx-community/sam2.1-hiera-*-ONNX` exports. The
`annotide-model-sam2` image bakes one in (`tools/fetch_sam2.py`).

Media is fetched from the signed URL the platform sends and never stored:
the cache holds encoder features only, in memory, for `SAM2_CACHE_ITEMS`
items (default 16, about 17 MB each).
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np
from PIL import Image

from app.backends.heuristic import fetch_image_bytes, load_image
from app.backends.masks import mask_to_polygon
from app.backends.onnx import SUPPORTED_PROVIDERS
from app.schemas import (
    EmbedItem,
    InteractiveRequest,
    InteractiveResponse,
    ItemEmbedding,
    ItemPrediction,
    LabelSchemaDefinition,
    MediaType,
    PredictItem,
)

ENCODER_FILE: Final = "vision_encoder.onnx"
DECODER_FILE: Final = "prompt_encoder_mask_decoder.onnx"
#: SAM2 squashes every image to this square, aspect ratio and all.
INPUT_SIZE: Final = 1024
#: The mask is traced at most this large (long side); larger images get the
#: polygon scaled up. The decoder's mask is 256 px anyway.
TRACE_SIZE: Final = 1024
DEFAULT_CACHE_ITEMS: Final = 16
_MEAN: Final = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD: Final = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_FEATURES: Final = ("image_embeddings.0", "image_embeddings.1", "image_embeddings.2")
_DECODER_INPUTS: Final = {"input_points", "input_labels", "input_boxes", *_FEATURES}

NO_OBJECT: Final = "SAM2 found no object at this prompt."


@dataclass(frozen=True, slots=True)
class _Features:
    """One item's encoder output and the size of the image it came from."""

    width: int
    height: int
    embeddings: tuple[np.ndarray, ...]


def preprocess(image: Image.Image) -> np.ndarray:
    """RGB image -> the encoder's `pixel_values`, `[1, 3, 1024, 1024]` float32."""
    resized = image.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
    pixels = np.asarray(resized, dtype=np.float32) / 255.0
    normalised = (pixels - _MEAN) / _STD
    return np.ascontiguousarray(normalised.transpose(2, 0, 1)[None])


class Sam2Backend:
    """Point and box prompts to polygons with a SAM2 encoder/decoder pair."""

    name: str
    task: str
    version: str
    gpu: bool
    media_types: list[MediaType]

    def __init__(self, model_path: str | None = None, *, cache_items: int | None = None) -> None:
        path_value = model_path or os.environ.get("MODEL_PATH")
        if not path_value:
            raise ValueError("MODEL_BACKEND=sam2 requires MODEL_PATH to point at a directory.")
        directory = Path(path_value)
        for file in (ENCODER_FILE, DECODER_FILE):
            if not (directory / file).is_file():
                # Fail at start-up: a container that reports healthy and then
                # fails every click is far harder to diagnose.
                raise ValueError(f"MODEL_PATH {directory} has no {file}.")
        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ValueError(
                "The sam2 backend needs onnxruntime (pip install annotide-model-service[onnx])."
            ) from exc

        available = set(onnxruntime.get_available_providers())
        providers = [p for p in SUPPORTED_PROVIDERS if p in available]
        self._encoder: Any = onnxruntime.InferenceSession(
            str(directory / ENCODER_FILE), providers=providers
        )
        self._decoder: Any = onnxruntime.InferenceSession(
            str(directory / DECODER_FILE), providers=providers
        )
        missing = _DECODER_INPUTS - {i.name for i in self._decoder.get_inputs()}
        if missing:
            raise ValueError(f"{DECODER_FILE} is not a SAM2 decoder: no {sorted(missing)}.")

        self.name = "sam2"
        self.task = "segment"
        self.version = directory.name
        self.gpu = any("CUDA" in provider for provider in providers)
        self.media_types = [MediaType.IMAGE]
        if cache_items is None:
            cache_items = int(os.environ.get("SAM2_CACHE_ITEMS", DEFAULT_CACHE_ITEMS))
        self._cache_items = max(1, cache_items)
        self._cache: OrderedDict[str, _Features] = OrderedDict()
        self._pending: dict[str, asyncio.Task[_Features]] = {}

    def classes(self) -> list[str]:
        # Class-agnostic: the annotator picks the class of what it outlines.
        return []

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        confidence_threshold: float,
    ) -> list[ItemPrediction]:
        raise ValueError("The sam2 backend segments on prompts; it does not pre-label.")

    async def embed(self, items: list[EmbedItem]) -> list[ItemEmbedding]:
        raise ValueError("The sam2 backend does not implement embeddings.")

    async def interactive(self, request: InteractiveRequest) -> InteractiveResponse:
        if request.point is None and request.box is None:
            raise ValueError("The sam2 backend takes a point or a box prompt, not text.")
        features = await self._features(request.item_id, request.url)
        width, height = features.width, features.height
        to_input_x, to_input_y = INPUT_SIZE / width, INPUT_SIZE / height

        prefer: tuple[float, float] | None = None
        if request.point is not None:
            prefer = (request.point.x, request.point.y)
            points = np.array([[[[request.point.x * to_input_x, request.point.y * to_input_y]]]])
            labels = np.ones((1, 1, 1), dtype=np.int64)
            boxes = np.zeros((1, 0, 4))
        else:
            assert request.box is not None
            x_min, y_min, x_max, y_max = request.box.bbox
            points = np.zeros((1, 1, 0, 2))
            labels = np.zeros((1, 1, 0), dtype=np.int64)
            boxes = np.array(
                [[[x_min * to_input_x, y_min * to_input_y, x_max * to_input_x, y_max * to_input_y]]]
            )
        feeds = {
            "input_points": points.astype(np.float32),
            "input_labels": labels,
            "input_boxes": boxes.astype(np.float32),
            **dict(zip(_FEATURES, features.embeddings, strict=True)),
        }
        iou, masks, objectness = await asyncio.to_thread(self._decoder.run, None, feeds)
        if float(objectness[0, 0, 0]) < 0:
            raise ValueError(NO_OBJECT)
        # One click is ambiguous (a shirt or the person?): take the mask the
        # model rates best. A box is not, and SAM2's first mask answers it.
        best = int(np.argmax(iou[0, 0])) if request.point is not None else 0

        scale = min(1.0, TRACE_SIZE / max(width, height))
        trace_w, trace_h = max(1, round(width * scale)), max(1, round(height * scale))
        logits = Image.fromarray(np.asarray(masks[0, 0, best], dtype=np.float32))
        mask = np.asarray(logits.resize((trace_w, trace_h), Image.Resampling.BILINEAR)) > 0
        to_trace_x, to_trace_y = trace_w / width, trace_h / height
        seed = (
            (int(prefer[0] * to_trace_x), int(prefer[1] * to_trace_y))
            if prefer is not None
            else None
        )
        polygon = mask_to_polygon(
            mask, scale_x=width / trace_w, scale_y=height / trace_h, prefer=seed
        )
        if not polygon:
            raise ValueError(NO_OBJECT)
        clamped = [(min(max(x, 0.0), width), min(max(y, 0.0), height)) for x, y in polygon]
        confidence = float(np.clip(iou[0, 0, best], 0.0, 1.0))
        return InteractiveResponse(points=clamped, confidence=confidence)

    async def _features(self, key: str, url: str) -> _Features:
        """The item's encoder output: cached, else computed once however many clicks wait."""
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._encode_url(key, url))
            self._pending[key] = task
            task.add_done_callback(lambda _: self._pending.pop(key, None))
        # Shielded: a click whose client went away does not cancel the others' wait.
        return await asyncio.shield(task)

    async def _encode_url(self, key: str, url: str) -> _Features:
        try:
            raw = await fetch_image_bytes(url)
            image = load_image(raw)
        except (httpx.HTTPError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f"could not load the item: {exc}") from exc
        features = await asyncio.to_thread(self._encode, image)
        self._cache[key] = features
        while len(self._cache) > self._cache_items:
            self._cache.popitem(last=False)
        return features

    def _encode(self, image: Image.Image) -> _Features:
        outputs = self._encoder.run(list(_FEATURES), {"pixel_values": preprocess(image)})
        return _Features(image.width, image.height, tuple(outputs))
