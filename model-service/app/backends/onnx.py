"""ONNX detection backend (BYOM: "model file upload — ONNX, TorchScript, safetensors").

Loads a detector from `MODEL_PATH` with onnxruntime. ONNX is chosen over pickle
formats deliberately: BYOM-8 forbids loading a customer-supplied model through
`pickle`, which executes arbitrary code on deserialisation. ONNX is a data
format, not a program.

The part worth reading carefully is the coordinate mapping. Detectors take a
fixed square input, so the image is letterboxed — scaled to fit, then padded to
centre it. Every predicted box is therefore in *letterboxed* pixels and must be
mapped back to original image pixels before it becomes an annotation. Forgetting
the padding offset produces boxes that are subtly, consistently wrong: they look
plausible, drift toward one corner, and nobody notices until the training run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from app.backends.heuristic import fetch_image_bytes, load_image, nms
from app.schemas import (
    AnnotationResult,
    BBoxShape,
    EmbedItem,
    InteractiveRequest,
    InteractiveResponse,
    ItemEmbedding,
    ItemPrediction,
    LabelSchemaDefinition,
    MediaType,
    PredictItem,
    Shape,
    ToolType,
)

DEFAULT_INPUT_SIZE = 640
SUPPORTED_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")


class LetterboxTransform:
    """Maps between original-image and letterboxed-input coordinates.

    Kept as its own object so the mapping can be unit-tested without a model
    file, which is the only practical way to be sure it is right.
    """

    __slots__ = ("pad_x", "pad_y", "scale")

    def __init__(self, width: int, height: int, size: int = DEFAULT_INPUT_SIZE) -> None:
        self.scale = min(size / max(width, 1), size / max(height, 1))
        # Padding is split evenly, so the image sits centred in the square.
        self.pad_x = (size - width * self.scale) / 2
        self.pad_y = (size - height * self.scale) / 2

    def to_input(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale + self.pad_x, y * self.scale + self.pad_y

    def to_original(self, x: float, y: float) -> tuple[float, float]:
        """Undo the letterbox: remove the padding first, then the scale."""
        return (x - self.pad_x) / self.scale, (y - self.pad_y) / self.scale

    def box_to_original(
        self, box: tuple[float, float, float, float], width: int, height: int
    ) -> tuple[float, float, float, float]:
        """Map a box back and clamp it inside the image."""
        x_min, y_min = self.to_original(box[0], box[1])
        x_max, y_max = self.to_original(box[2], box[3])
        x_min, x_max = sorted((x_min, x_max))
        y_min, y_max = sorted((y_min, y_max))
        return (
            max(0.0, min(x_min, float(width))),
            max(0.0, min(y_min, float(height))),
            max(0.0, min(x_max, float(width))),
            max(0.0, min(y_max, float(height))),
        )


def letterbox(image: Any, size: int = DEFAULT_INPUT_SIZE) -> tuple[np.ndarray, LetterboxTransform]:
    """Scale-and-pad an image into a square input tensor."""
    from PIL import Image

    transform = LetterboxTransform(image.width, image.height, size)
    new_w = max(1, round(image.width * transform.scale))
    new_h = max(1, round(image.height * transform.scale))

    resized = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(resized, (int(transform.pad_x), int(transform.pad_y)))

    array = np.asarray(canvas, dtype=np.float32) / 255.0
    # NCHW, the layout virtually every exported detector expects.
    return np.transpose(array, (2, 0, 1))[None, ...], transform


class OnnxBackend:
    """A detector loaded from an ONNX file."""

    name: str
    version: str
    gpu: bool
    media_types: list[MediaType]

    def __init__(self, model_path: str | None = None) -> None:
        path_value = model_path or os.environ.get("MODEL_PATH")
        if not path_value:
            raise ValueError("MODEL_BACKEND=onnx requires MODEL_PATH to point at an .onnx file.")
        path = Path(path_value)
        if not path.is_file():
            # Fail at start-up, not on the first prediction: a container that
            # reports healthy and then 500s on use is far harder to diagnose.
            raise ValueError(f"MODEL_PATH {path} does not exist or is not a file.")

        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ValueError(
                "The onnx backend needs onnxruntime (pip install annotide-model-service[onnx])."
            ) from exc

        self.name = "onnx"
        # CUDA when present, else CPU. Not every available provider: CoreML
        # (macOS) cannot run a detector's zero-detection branch, whose
        # intermediate tensors have no rows.
        available = set(onnxruntime.get_available_providers())
        providers = [p for p in SUPPORTED_PROVIDERS if p in available]
        self._session = onnxruntime.InferenceSession(str(path), providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        self.gpu = any("CUDA" in provider for provider in providers)
        self.version = path.stem
        self.media_types = [MediaType.IMAGE]

        # Class names come from a sibling .names file if present; otherwise the
        # platform's class mapping (BYOM-2) supplies them.
        names_file = path.with_suffix(".names")
        self._class_names: list[str] = (
            [line.strip() for line in names_file.read_text().splitlines() if line.strip()]
            if names_file.is_file()
            else []
        )

    def classes(self) -> list[str]:
        return list(self._class_names)

    def _allowed_classes(self, schema: LabelSchemaDefinition) -> list[str]:
        """Schema classes that permit a bounding box, in schema order."""
        return [cls.name for cls in schema.classes if ToolType.BBOX in cls.tools]

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        confidence_threshold: float,
    ) -> list[ItemPrediction]:
        allowed = self._allowed_classes(schema)
        predictions: list[ItemPrediction] = []

        for item in items:
            try:
                raw = await fetch_image_bytes(item.url)
                image = load_image(raw)
                shapes = self._detect(image, item, allowed, confidence_threshold)
            except Exception as exc:  # one bad item must not fail the batch
                predictions.append(
                    ItemPrediction(
                        item_id=item.id,
                        result=AnnotationResult(
                            schema_version=schema.version,
                            media_type=MediaType.IMAGE,
                            classification={},
                            shapes=[],
                        ),
                        confidence=0.0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            confidences = [s.confidence or 0.0 for s in shapes]
            predictions.append(
                ItemPrediction(
                    item_id=item.id,
                    result=AnnotationResult(
                        schema_version=schema.version,
                        media_type=MediaType.IMAGE,
                        classification={},
                        shapes=shapes,
                    ),
                    confidence=max(confidences) if confidences else 0.0,
                )
            )
        return predictions

    def _detect(
        self,
        image: Any,
        item: PredictItem,
        allowed: list[str],
        threshold: float,
    ) -> list[Shape]:
        if not allowed:
            # The schema defines no class that accepts a box. Emitting one
            # anyway would introduce a class the project never defined.
            return []

        tensor, transform = letterbox(image)
        outputs = self._session.run(None, {self._input_name: tensor})
        boxes, scores, class_ids = _decode(outputs)

        keep = nms([tuple(float(v) for v in b) for b in boxes], [float(s) for s in scores])  # type: ignore[misc]

        shapes: list[Shape] = []
        for index in keep:
            score = float(scores[index])
            if score < threshold:
                continue
            mapped = transform.box_to_original(
                tuple(float(v) for v in boxes[index]),  # type: ignore[arg-type]
                item.width,
                item.height,
            )
            if mapped[2] - mapped[0] < 1 or mapped[3] - mapped[1] < 1:
                continue

            class_index = int(class_ids[index])
            name = (
                self._class_names[class_index]
                if 0 <= class_index < len(self._class_names)
                else None
            )
            # Only classes the project actually defines (BYOM-2).
            if name not in allowed:
                name = allowed[class_index % len(allowed)] if not self._class_names else None
            if name is None:
                continue

            shapes.append(
                BBoxShape(
                    id=uuid4(),
                    type="bbox",
                    **{"class": name},
                    attributes={},
                    confidence=min(1.0, max(0.0, score)),
                    bbox=mapped,
                )
            )
        return shapes

    async def interactive(self, request: InteractiveRequest) -> InteractiveResponse:
        raise ValueError("The onnx backend does not implement interactive segmentation.")

    async def embed(self, items: list[EmbedItem]) -> list[ItemEmbedding]:
        raise ValueError("The onnx backend does not implement embeddings.")


def _decode(outputs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pull boxes, scores and class ids out of a detector's raw output.

    Handles the two common export shapes: separate (boxes, scores, classes)
    tensors, and a single `(N, 6)` array of `[x1, y1, x2, y2, score, class]`.
    """
    if len(outputs) >= 3:
        boxes = np.asarray(outputs[0]).reshape(-1, 4)
        scores = np.asarray(outputs[1]).reshape(-1)
        classes = np.asarray(outputs[2]).reshape(-1)
        return boxes, scores, classes

    single = np.asarray(outputs[0])
    single = single.reshape(-1, single.shape[-1])
    if single.shape[-1] < 6:
        raise ValueError(f"unsupported ONNX output shape {single.shape}")
    return single[:, :4], single[:, 4], single[:, 5]
