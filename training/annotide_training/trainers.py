"""The pluggable part: a `Trainer` turns records into a model and predicts with it.

A real trainer (a detector fine-tune exported to ONNX for
`model-service`'s `onnx` backend, say) implements the same two methods and
is loaded by `--trainer package.module:ClassName`; its framework is its own
dependency. `BaselineTrainer` needs nothing: it learns where each class
usually sits in the frame, which is enough to exercise every stage of the
pipeline end to end and gives a floor any real model has to beat.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

Record = Mapping[str, Any]
Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels


@dataclass(frozen=True, slots=True)
class Prediction:
    class_: str
    bbox: Box
    confidence: float


@dataclass(slots=True)
class TrainedModel:
    """What a trainer hands back: an artifact to store and what it can emit."""

    artifact: bytes
    filename: str
    classes: list[str]
    info: dict[str, Any] = field(default_factory=dict)
    #: Files written next to the artifact, by name (an ONNX model's `.names`).
    extra_files: dict[str, bytes] = field(default_factory=dict)


@runtime_checkable
class Trainer(Protocol):
    name: str

    def train(
        self, train: Sequence[Record], val: Sequence[Record], params: Mapping[str, Any]
    ) -> TrainedModel: ...

    def predict(self, model: TrainedModel, record: Record) -> list[Prediction]: ...


#: What `--quantize` accepts. int8 is the one every CPU runtime speeds up.
QUANTIZE_DTYPES = ("int8",)


@runtime_checkable
class Quantizer(Protocol):
    """Optional: a trainer that can store a trained model at lower precision.

    `calibration` is a sample of training records for static quantization
    (activation ranges); the result must load and predict through the same
    trainer's `predict`, so the pipeline scores it exactly like the original.
    """

    def quantize(
        self, model: TrainedModel, calibration: Sequence[Record], dtype: str
    ) -> TrainedModel: ...


def record_boxes(record: Record) -> list[tuple[str, Box]]:
    """The `bbox` shapes of a native export record, as (class, x1 y1 x2 y2)."""
    boxes: list[tuple[str, Box]] = []
    for shape in record.get("shapes", []):
        bbox = shape.get("bbox")
        if shape.get("type") == "bbox" and isinstance(bbox, list) and len(bbox) == 4:
            x1, y1, x2, y2 = (float(v) for v in bbox)
            boxes.append((str(shape.get("class")), (x1, y1, x2, y2)))
    return boxes


class BaselineTrainer:
    """Per class: how often it appears and its mean normalised box.

    Predicts, for every class seen in at least `min_frequency` of the
    training images, one box at that mean position with the frequency as
    its confidence. Deterministic, dependency-free, and honest about being
    a baseline.
    """

    name = "baseline-class-prior"

    def train(
        self, train: Sequence[Record], val: Sequence[Record], params: Mapping[str, Any]
    ) -> TrainedModel:
        min_frequency = float(params.get("min_frequency", 0.0))
        images = 0
        present: dict[str, int] = {}
        sums: dict[str, list[float]] = {}
        counts: dict[str, int] = {}
        for record in train:
            width = float(record.get("width") or 0)
            height = float(record.get("height") or 0)
            if width <= 0 or height <= 0:
                continue
            images += 1
            seen: set[str] = set()
            for class_name, (x1, y1, x2, y2) in record_boxes(record):
                acc = sums.setdefault(class_name, [0.0, 0.0, 0.0, 0.0])
                for i, value in enumerate((x1 / width, y1 / height, x2 / width, y2 / height)):
                    acc[i] += value
                counts[class_name] = counts.get(class_name, 0) + 1
                seen.add(class_name)
            for class_name in seen:
                present[class_name] = present.get(class_name, 0) + 1

        priors: dict[str, dict[str, Any]] = {
            name: {
                "frequency": round(present.get(name, 0) / images, 6) if images else 0.0,
                "box": [round(v / counts[name], 6) for v in sums[name]],
            }
            for name in sorted(sums)
        }
        emitted = sorted(n for n, p in priors.items() if p["frequency"] >= min_frequency)
        body = {
            "trainer": self.name,
            "images": images,
            "min_frequency": min_frequency,
            "priors": {n: priors[n] for n in emitted},
        }
        return TrainedModel(
            artifact=json.dumps(body, indent=2, sort_keys=True).encode(),
            filename="baseline.json",
            classes=emitted,
            info={"train_images": images, "val_images": len(val), "dtype": "fp64"},
        )

    def quantize(
        self, model: TrainedModel, calibration: Sequence[Record], dtype: str
    ) -> TrainedModel:
        """Each prior's box and frequency as an 8-bit fraction (k / 255).

        The baseline's floats are already tiny; this exists so the
        `--quantize` path runs end to end without the torch extra.
        """
        if dtype != "int8":
            raise ValueError(f"{self.name} quantizes to int8 only, not {dtype!r}")
        body = json.loads(model.artifact)
        for prior in body["priors"].values():
            prior["box"] = [_to_uint8(v) for v in prior["box"]]
            prior["frequency"] = _to_uint8(prior["frequency"])
        body["dtype"] = "int8"
        return TrainedModel(
            artifact=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
            filename=model.filename,
            classes=list(model.classes),
            info={**model.info, "dtype": "int8"},
            extra_files=dict(model.extra_files),
        )

    def predict(self, model: TrainedModel, record: Record) -> list[Prediction]:
        body = json.loads(model.artifact)
        scale = 255.0 if body.get("dtype") == "int8" else 1.0
        width = float(record.get("width") or 0)
        height = float(record.get("height") or 0)
        predictions: list[Prediction] = []
        for class_name, prior in body["priors"].items():
            nx1, ny1, nx2, ny2 = (v / scale for v in prior["box"])
            predictions.append(
                Prediction(
                    class_=class_name,
                    bbox=(nx1 * width, ny1 * height, nx2 * width, ny2 * height),
                    confidence=float(prior["frequency"]) / scale,
                )
            )
        return predictions


def _to_uint8(value: float) -> int:
    return max(0, min(255, round(value * 255)))


# Built-ins by name. `fasterrcnn` needs the `torch` extra, so it is imported
# only when asked for.
BUILTIN_TRAINERS: dict[str, str] = {
    "baseline": "annotide_training.trainers:BaselineTrainer",
    "fasterrcnn": "annotide_training.detector:FasterRCNNTrainer",
}
_TORCH_EXTRA = {"torch", "torchvision", "onnx", "onnxruntime", "numpy", "PIL"}


def load_trainer(spec: str) -> Trainer:
    """`baseline`, `fasterrcnn`, or `package.module:ClassName` for your own."""
    builtin = BUILTIN_TRAINERS.get(spec)
    try:
        return _load(builtin or spec)
    except ModuleNotFoundError as exc:
        if builtin and exc.name in _TORCH_EXTRA:
            raise ValueError(
                f"trainer {spec!r} needs the torch extra: pip install 'annotide-training[torch]'"
            ) from exc
        raise


def _load(spec: str) -> Trainer:
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"trainer {spec!r}: use a built-in name or 'module:ClassName'")
    factory = getattr(importlib.import_module(module_name), attr)
    trainer = factory()
    if not isinstance(trainer, Trainer):
        raise TypeError(f"{spec} does not implement Trainer (name, train, predict)")
    return trainer
