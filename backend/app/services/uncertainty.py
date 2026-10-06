"""Uncertainty of a model prediction, for active learning (ML-6).

The pre-label job scores every prediction and, when asked, writes the score
into the item's open `annotate` task as its priority, so `POST /tasks/next`
(WF-6: `priority DESC`) hands annotators the items the model was least sure
about first — where a human label teaches the next model version the most.
"""

from __future__ import annotations

from app.schemas import AnnotationResult

#: Task priority written for a score of 1.0; scores map linearly onto 0..100
#: so hand-set priorities (WF-6, usually small integers) can still outrank
#: them by using a larger number.
UNCERTAINTY_PRIORITY_SCALE = 100

#: Score for a shape whose model gave no confidence at all.
_UNKNOWN_CONFIDENCE_SCORE = 0.5


def uncertainty_score(result: AnnotationResult | None) -> float:
    """Least-confidence score in [0, 1]: `1 - min(confidence)` over the shapes.

    An empty prediction (`None`, or no shapes) scores 1.0: the model either
    saw nothing or missed everything, and only a human can tell which. Shapes
    without a `confidence` count as 0.5 — unknown, neither sure nor unsure.
    The minimum rather than the mean, because one doubtful box is enough to
    make the whole image worth a look.
    """
    if result is None or not result.shapes:
        return 1.0
    confidences = [shape.confidence for shape in result.shapes if shape.confidence is not None]
    if not confidences:
        return _UNKNOWN_CONFIDENCE_SCORE
    return round(1.0 - min(confidences), 4)


def uncertainty_priority(score: float) -> int:
    """Task priority for a score: 0..`UNCERTAINTY_PRIORITY_SCALE`."""
    return round(max(0.0, min(1.0, score)) * UNCERTAINTY_PRIORITY_SCALE)
