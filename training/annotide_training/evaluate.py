"""Evaluate: box precision / recall / F1 at an IoU threshold, per class and overall.

Also the median time `predict` takes per item, so a quantized or distilled
version can be compared with its parent on speed as well as accuracy.

Framework-independent, so every trainer is scored the same way on the same
held-out split. Greedy matching by confidence, one prediction per ground
truth box, classes never cross.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from typing import Any

from annotide_training.trainers import Box, Record, TrainedModel, Trainer, record_boxes


def iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def evaluate(
    trainer: Trainer,
    model: TrainedModel,
    records: Sequence[Record],
    *,
    iou_threshold: float = 0.5,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    tallies: dict[str, list[int]] = {}  # class -> [tp, fp, fn]
    latencies: list[float] = []
    for record in records:
        truths = record_boxes(record)
        started = time.perf_counter()
        raw = trainer.predict(model, record)
        latencies.append(time.perf_counter() - started)
        predictions = sorted(
            (p for p in raw if p.confidence >= min_confidence),
            key=lambda p: -p.confidence,
        )
        matched: set[int] = set()
        for prediction in predictions:
            tally = tallies.setdefault(prediction.class_, [0, 0, 0])
            best, best_iou = -1, iou_threshold
            for index, (class_name, box) in enumerate(truths):
                if index in matched or class_name != prediction.class_:
                    continue
                overlap = iou(prediction.bbox, box)
                if overlap >= best_iou:
                    best, best_iou = index, overlap
            if best >= 0:
                matched.add(best)
                tally[0] += 1
            else:
                tally[1] += 1
        for index, (class_name, _) in enumerate(truths):
            if index not in matched:
                tallies.setdefault(class_name, [0, 0, 0])[2] += 1

    totals = [sum(t[i] for t in tallies.values()) for i in range(3)]
    return {
        "iou_threshold": iou_threshold,
        "images": len(records),
        # Per item, reading the image included, on the machine that ran this.
        "latency_ms_p50": round(statistics.median(latencies) * 1000) if latencies else None,
        **_prf(*totals),
        "per_class": {name: _prf(*tallies[name]) for name in sorted(tallies)},
    }
