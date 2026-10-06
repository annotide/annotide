"""A real trainer: Faster R-CNN (MobileNetV3-FPN) fine-tuned and exported to ONNX.

`--trainer fasterrcnn`, with the `torch` extra installed. The artifact is a
`.onnx` file plus a sibling `.names` file, in exactly the shape
`model-service`'s `onnx` backend loads (`MODEL_BACKEND=onnx`,
`MODEL_PATH=…/fasterrcnn.onnx`): one `1x3x640x640` float input in `[0, 1]`,
letterboxed with grey padding, and three outputs — boxes (`x1 y1 x2 y2` in
letterboxed pixels), scores, and 0-based class indices into the `.names`
lines. Normalisation and NMS are inside the graph.

Why torchvision: its code is BSD-3-Clause, so a customer can ship the
trainer and the model in a commercial product. Ultralytics YOLO, the obvious
alternative, is AGPL-3.0. The COCO weights used for `weights=coco` are
torchvision's own; check their terms against your use before shipping a
model fine-tuned from them, or train from `imagenet` / `none`.

Media never passes through the platform: records carry only the item's
`path` in the customer's storage, so the trainer reads images from
`image_root` — a directory where that storage is mounted or synced
(blobfuse2, gcsfuse, `aws s3 sync`, …).

`predict` runs the exported ONNX file through onnxruntime rather than the
torch model, so the metrics the pipeline registers are for the artifact that
ships, including the export and the letterbox.
"""

from __future__ import annotations

import random
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime
import torch
from onnxruntime.quantization import (
    CalibrationDataReader,
    QuantFormat,
    QuantType,
    quantize_static,
)
from onnxruntime.quantization.shape_inference import quant_pre_process
from PIL import Image
from torchvision.models import MobileNet_V3_Large_Weights
from torchvision.models.detection import (
    FasterRCNN_MobileNet_V3_Large_FPN_Weights,
    fasterrcnn_mobilenet_v3_large_fpn,
)
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

from annotide_training.trainers import Box, Prediction, Record, TrainedModel, record_boxes

# Both must match model-service/app/backends/onnx.py (`letterbox`).
INPUT_SIZE = 640
PAD_GREY = 114
WEIGHTS = ("coco", "imagenet", "none")


@dataclass(frozen=True, slots=True)
class Letterbox:
    """Original-image ↔ letterboxed-input coordinates (scale, then centre-pad)."""

    scale: float
    pad_x: float
    pad_y: float

    @classmethod
    def fit(cls, width: int, height: int, size: int = INPUT_SIZE) -> Letterbox:
        scale = min(size / max(width, 1), size / max(height, 1))
        return cls(scale, (size - width * scale) / 2, (size - height * scale) / 2)

    def box_to_input(self, box: Box) -> Box:
        x1, y1, x2, y2 = box
        s, px, py = self.scale, self.pad_x, self.pad_y
        return (x1 * s + px, y1 * s + py, x2 * s + px, y2 * s + py)

    def box_to_original(self, box: Box, width: int, height: int) -> Box:
        x1, y1, x2, y2 = box
        s, px, py = self.scale, self.pad_x, self.pad_y
        xs = sorted(((x1 - px) / s, (x2 - px) / s))
        ys = sorted(((y1 - py) / s, (y2 - py) / s))
        return (
            max(0.0, min(xs[0], float(width))),
            max(0.0, min(ys[0], float(height))),
            max(0.0, min(xs[1], float(width))),
            max(0.0, min(ys[1], float(height))),
        )


def letterbox(image: Image.Image, size: int = INPUT_SIZE) -> tuple[np.ndarray, Letterbox]:
    """A `3xsizexsize` float32 array in `[0, 1]` and the transform that made it."""
    transform = Letterbox.fit(image.width, image.height, size)
    new_w = max(1, round(image.width * transform.scale))
    new_h = max(1, round(image.height * transform.scale))
    resized = image.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (size, size), (PAD_GREY, PAD_GREY, PAD_GREY))
    canvas.paste(resized, (int(transform.pad_x), int(transform.pad_y)))
    array = np.asarray(canvas, dtype=np.float32) / 255.0
    return np.ascontiguousarray(np.transpose(array, (2, 0, 1))), transform


class ImageRoot:
    """Reads a record's image from a local directory, never outside it."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"image_root {self.root} is not a directory")

    def path(self, record: Record) -> Path:
        relative = str(record.get("path") or "").lstrip("/")
        path = (self.root / relative).resolve()
        # An export record is data from elsewhere: a `../` must not walk out.
        if not relative or not path.is_relative_to(self.root):
            raise ValueError(f"item {record.get('item_id')}: path {relative!r} escapes image_root")
        return path

    def open(self, record: Record) -> Image.Image:
        with Image.open(self.path(record)) as image:
            image.load()
            return image.convert("RGB")

    def check(self, records: Sequence[Record]) -> None:
        """Fail before training, not an hour into it, when images are missing."""
        missing = [str(r.get("path")) for r in records if not self.path(r).is_file()]
        if missing:
            shown = ", ".join(missing[:5]) + (
                f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            )
            raise FileNotFoundError(f"{len(missing)} image(s) not under {self.root}: {shown}")


def build_model(num_classes: int, weights: str) -> torch.nn.Module:
    """Faster R-CNN with `num_classes` foreground classes (+ background)."""
    if weights not in WEIGHTS:
        raise ValueError(f"weights {weights!r}: use one of {', '.join(WEIGHTS)}")
    # No RPN score floor (torchvision's MobileNet default is 0.05): an image
    # with no proposals left reaches a `Reshape(0, -1)` the ONNX graph cannot run.
    size: dict[str, Any] = {"min_size": INPUT_SIZE, "max_size": INPUT_SIZE, "rpn_score_thresh": 0.0}
    model: torch.nn.Module
    if weights == "coco":
        coco: Any = fasterrcnn_mobilenet_v3_large_fpn(
            weights=FasterRCNN_MobileNet_V3_Large_FPN_Weights.COCO_V1, **size
        )
        in_features = coco.roi_heads.box_predictor.cls_score.in_features
        coco.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes + 1)
        model = coco
        return model
    backbone = MobileNet_V3_Large_Weights.IMAGENET1K_V1 if weights == "imagenet" else None
    model = fasterrcnn_mobilenet_v3_large_fpn(
        weights=None, weights_backbone=backbone, num_classes=num_classes + 1, **size
    )
    return model


class _OnnxExport(torch.nn.Module):
    """Batch-of-one tensor in, (boxes, scores, 0-based labels) out."""

    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, images: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        detections = self.model([images[0]])[0]
        # torchvision reserves label 0 for background; the .names file does not.
        return detections["boxes"], detections["scores"], detections["labels"] - 1


def export_onnx(model: torch.nn.Module, samples: Sequence[np.ndarray]) -> bytes:
    """Trace the model to ONNX on a real image and check it reproduces torch.

    The trace input matters: traced on an image with no detections (noise, a
    blank frame), the ROI head's box reshape is folded into a constant and the
    graph fails on any image that has some. So each candidate is traced on a
    training image and accepted only when onnxruntime matches torch on all the
    samples and on a blank frame (nothing to detect).
    """
    if not samples:
        raise ValueError("export needs at least one sample image")
    model.eval().cpu()
    blank = np.full((3, INPUT_SIZE, INPUT_SIZE), PAD_GREY / 255.0, dtype=np.float32)
    checks = [*samples, blank]
    problem = ""
    for trace in samples[:3]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.onnx"
            # The TorchScript exporter: torchvision's detection models are
            # written for it. `.eval()` on the wrapper too, or export restores
            # it — and the detector inside — to training mode afterwards.
            torch.onnx.export(
                _OnnxExport(model).eval(),
                (torch.from_numpy(trace)[None],),
                path,
                input_names=["images"],
                output_names=["boxes", "scores", "labels"],
                dynamic_axes={"boxes": {0: "n"}, "scores": {0: "n"}, "labels": {0: "n"}},
                opset_version=17,
                dynamo=False,
            )
            artifact = path.read_bytes()
        problem = _mismatch(model, artifact, checks)
        if not problem:
            return artifact
    raise RuntimeError(f"the ONNX export does not reproduce the torch model: {problem}")


def _mismatch(model: torch.nn.Module, artifact: bytes, samples: Sequence[np.ndarray]) -> str:
    """Empty when onnxruntime gives the torch model's detections on every sample."""
    session = _session(artifact)
    for index, sample in enumerate(samples):
        with torch.no_grad():
            expected = model([torch.from_numpy(sample)])[0]
        try:
            boxes, scores, _ = session.run(None, {"images": sample[None, ...]})
        except Exception as exc:  # onnxruntime raises its own Fail type
            return f"sample {index}: {type(exc).__name__}: {exc}"
        want_scores = expected["scores"].numpy()
        want_boxes = expected["boxes"].numpy()
        if len(scores) != len(want_scores):
            return f"sample {index}: {len(scores)} detections, torch has {len(want_scores)}"
        if not len(scores):
            continue
        # Among near-equal scores, float noise decides which proposals survive
        # top-k and NMS, differently in each runtime (a barely trained head
        # scores everything alike). So only boxes whose score stands clear of
        # every other are compared, each to its nearest counterpart.
        distance = np.abs(boxes[:, None, :] - want_boxes[None, :, :]).max(axis=-1)
        own, want_own = _untied(scores), _untied(want_scores)
        score_gap = float(np.abs(np.sort(scores) - np.sort(want_scores)).max())
        box_gap = max(
            float(distance[own].min(axis=1).max()) if own.any() else 0.0,
            float(distance[:, want_own].min(axis=0).max()) if want_own.any() else 0.0,
        )
        if score_gap > 1e-3 or box_gap > 0.5:
            return f"sample {index}: score differs by {score_gap:.2g}, a box by {box_gap:.2g} px"
    return ""


def _untied(scores: np.ndarray) -> np.ndarray:
    """Mask of the scores no other score is within 1e-3 of."""
    gaps = np.abs(scores[:, None] - scores[None, :]) < 1e-3
    return np.asarray(gaps.sum(axis=1) == 1)


class _Calibration(CalibrationDataReader):  # type: ignore[misc]  # onnxruntime is untyped
    """Letterboxed training images, one batch of one at a time."""

    def __init__(self, samples: Sequence[np.ndarray]) -> None:
        self._samples = iter(samples)

    def get_next(self) -> dict[str, np.ndarray] | None:
        sample = next(self._samples, None)
        return None if sample is None else {"images": sample[None, ...]}


def quantize_onnx(artifact: bytes, samples: Sequence[np.ndarray]) -> bytes:
    """Static int8 (QDQ) quantization, calibrated on real letterboxed images.

    Conv covers the backbone and FPN; Gemm / MatMul the box head's two fully
    connected layers, which hold most of the weights. NMS, RoiAlign and the
    box decoding stay float. Weights per channel, activations from the
    calibration images' ranges, so they must look like what the model will see.
    """
    if not samples:
        raise ValueError("int8 quantization needs at least one calibration image")
    with tempfile.TemporaryDirectory() as tmp:
        source, prepared, output = (Path(tmp) / n for n in ("in.onnx", "pre.onnx", "q.onnx"))
        source.write_bytes(artifact)
        # Shape inference and constant folding so the quantizer sees every Conv;
        # the symbolic pass fails on the detection head's dynamic shapes.
        quant_pre_process(str(source), str(prepared), skip_symbolic_shape=True)
        quantize_static(
            str(prepared),
            str(output),
            _Calibration(samples),
            quant_format=QuantFormat.QDQ,
            per_channel=True,
            activation_type=QuantType.QUInt8,
            weight_type=QuantType.QInt8,
            op_types_to_quantize=["Conv", "Gemm", "MatMul"],
        )
        return output.read_bytes()


def _session(artifact: bytes) -> Any:
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3  # the exported graph's shape warnings are noise
    return onnxruntime.InferenceSession(artifact, options, providers=["CPUExecutionProvider"])


class FasterRCNNTrainer:
    """Fine-tunes Faster R-CNN on the snapshot's boxes and exports it to ONNX.

    `--quantize int8` stores the exported graph as static int8 (QDQ),
    calibrated on up to `calibration_images` (32) training images.

    Params (`--param key=value`): `image_root` (required unless given to the
    constructor), `epochs` (10), `batch_size` (4), `lr` (0.01, SGD with
    cosine decay), `weights` (`coco` | `imagenet` | `none`), `hflip` (true),
    `seed` (0), `device` (`cuda` when available, else `cpu`),
    `score_threshold` (0.5, applied in `predict` and so in the metrics).
    """

    name = "fasterrcnn-mobilenet-v3-onnx"

    def __init__(self, image_root: str | Path | None = None) -> None:
        self._images = ImageRoot(image_root) if image_root is not None else None
        self._score_threshold = 0.5
        self._calibration_images = 32
        self._session: tuple[bytes, Any] | None = None

    def train(
        self, train: Sequence[Record], val: Sequence[Record], params: Mapping[str, Any]
    ) -> TrainedModel:
        if "image_root" in params:
            self._images = ImageRoot(str(params["image_root"]))
        if self._images is None:
            raise ValueError("fasterrcnn needs image_root: where the item paths are mounted")
        images = self._images
        epochs = int(params.get("epochs", 10))
        batch_size = int(params.get("batch_size", 4))
        lr = float(params.get("lr", 0.01))
        weights = str(params.get("weights", "coco"))
        hflip = bool(params.get("hflip", True))
        seed = int(params.get("seed", 0))
        device = torch.device(
            str(params.get("device") or ("cuda" if torch.cuda.is_available() else "cpu"))
        )
        self._score_threshold = float(params.get("score_threshold", 0.5))
        self._calibration_images = int(params.get("calibration_images", 32))
        if epochs < 1 or batch_size < 1:
            raise ValueError("epochs and batch_size must be at least 1")

        classes = sorted({name for record in train for name, _ in record_boxes(record)})
        if not classes:
            raise ValueError("the train split has no bbox shapes to learn from")
        images.check([*train, *val])

        torch.manual_seed(seed)
        rng = random.Random(seed)
        model = build_model(len(classes), weights).to(device)
        model.train()
        trainable = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.SGD(trainable, lr=lr, momentum=0.9, weight_decay=5e-4)
        steps = epochs * -(-len(train) // batch_size)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=steps)

        epoch_losses: list[float] = []
        order = list(range(len(train)))
        for _ in range(epochs):
            rng.shuffle(order)
            total, batches = 0.0, 0
            for start in range(0, len(order), batch_size):
                inputs, targets = [], []
                for index in order[start : start + batch_size]:
                    flip = hflip and rng.random() < 0.5
                    array, target = self._sample(images, train[index], classes, flip)
                    inputs.append(torch.from_numpy(array).to(device))
                    targets.append({k: v.to(device) for k, v in target.items()})
                losses = model(inputs, targets)
                loss = torch.stack(list(losses.values())).sum()
                optimizer.zero_grad()
                torch.autograd.backward(loss)
                optimizer.step()
                scheduler.step()
                total += float(loss.detach())
                batches += 1
            epoch_losses.append(round(total / batches, 4))

        # Trace on the images with the most boxes: they are sure to detect something.
        busiest = sorted(train, key=lambda r: -len(record_boxes(r)))[:4]
        artifact = export_onnx(model, [letterbox(images.open(r))[0] for r in busiest])
        return TrainedModel(
            artifact=artifact,
            filename="fasterrcnn.onnx",
            classes=classes,
            info={
                "architecture": "fasterrcnn_mobilenet_v3_large_fpn",
                "weights": weights,
                "input_size": INPUT_SIZE,
                "epochs": epochs,
                "epoch_losses": epoch_losses,
                "train_images": len(train),
                "val_images": len(val),
                "score_threshold": self._score_threshold,
                "onnx_bytes": len(artifact),
                "dtype": "fp32",
            },
            extra_files={"fasterrcnn.names": ("\n".join(classes) + "\n").encode()},
        )

    def quantize(
        self, model: TrainedModel, calibration: Sequence[Record], dtype: str
    ) -> TrainedModel:
        if dtype != "int8":
            raise ValueError(f"{self.name} quantizes to int8 only, not {dtype!r}")
        if self._images is None:
            raise ValueError("fasterrcnn needs image_root to calibrate")
        images = self._images
        samples = [letterbox(images.open(r))[0] for r in calibration[: self._calibration_images]]
        artifact = quantize_onnx(model.artifact, samples)
        return TrainedModel(
            artifact=artifact,
            filename=model.filename,
            classes=list(model.classes),
            info={
                **model.info,
                "dtype": "int8",
                "quantization": "static QDQ, per-channel weights; Conv, Gemm, MatMul",
                "calibration_images": len(samples),
                "onnx_bytes": len(artifact),
            },
            extra_files=dict(model.extra_files),
        )

    def _sample(
        self, images: ImageRoot, record: Record, classes: list[str], flip: bool
    ) -> tuple[np.ndarray, dict[str, torch.Tensor]]:
        image = images.open(record)
        # Boxes are in the record's pixel frame; rescale if the file differs.
        sx = image.width / float(record.get("width") or image.width)
        sy = image.height / float(record.get("height") or image.height)
        array, transform = letterbox(image)
        boxes: list[Box] = []
        labels: list[int] = []
        for name, (x1, y1, x2, y2) in record_boxes(record):
            if name not in classes:
                continue
            bx1, by1, bx2, by2 = transform.box_to_input((x1 * sx, y1 * sy, x2 * sx, y2 * sy))
            if flip:
                bx1, bx2 = INPUT_SIZE - bx2, INPUT_SIZE - bx1
            # Faster R-CNN rejects zero-area targets.
            if bx2 - bx1 >= 1 and by2 - by1 >= 1:
                boxes.append((bx1, by1, bx2, by2))
                labels.append(classes.index(name) + 1)
        if flip:
            array = np.ascontiguousarray(array[:, :, ::-1])
        return array, {
            "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            "labels": torch.tensor(labels, dtype=torch.int64),
        }

    def predict(self, model: TrainedModel, record: Record) -> list[Prediction]:
        if self._images is None:
            raise ValueError("fasterrcnn needs image_root to predict")
        session = self._onnx_session(model.artifact)
        image = self._images.open(record)
        array, transform = letterbox(image)
        boxes, scores, labels = session.run(None, {"images": array[None, ...]})
        width = int(record.get("width") or image.width)
        height = int(record.get("height") or image.height)
        sx, sy = width / image.width, height / image.height
        predictions: list[Prediction] = []
        for box, score, label in zip(boxes, scores, labels, strict=True):
            if float(score) < self._score_threshold or not 0 <= int(label) < len(model.classes):
                continue
            x1, y1, x2, y2 = transform.box_to_original(
                (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                image.width,
                image.height,
            )
            predictions.append(
                Prediction(
                    class_=model.classes[int(label)],
                    bbox=(x1 * sx, y1 * sy, x2 * sx, y2 * sy),
                    confidence=float(score),
                )
            )
        return predictions

    def _onnx_session(self, artifact: bytes) -> Any:
        if self._session is None or self._session[0] is not artifact:
            self._session = (artifact, _session(artifact))
        return self._session[1]
