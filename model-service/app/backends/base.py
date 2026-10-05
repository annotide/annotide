"""The `ModelBackend` protocol every backend implements.

This is the BYOM-4 template contract: bring your own model by writing a class
that satisfies `ModelBackend`, point `MODEL_BACKEND` at it (see
`app/backends/__init__.py::get_backend`), and the FastAPI app in
`app/main.py` needs no other changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class ModelBackend(Protocol):
    """A pluggable model implementation behind the four §8 endpoints.

    Implementations own all the heavy lifting (fetching images, inference,
    postprocessing); `app/main.py` only validates request/response shapes and
    applies the schema-driven class filter shared by every backend.
    """

    name: str
    version: str
    gpu: bool
    media_types: list[MediaType]

    def classes(self) -> list[str]:
        """Class names this model can produce (before any per-request schema filter)."""
        ...

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        confidence_threshold: float,
    ) -> list[ItemPrediction]:
        """Run detection on each item, filtered to classes/tools the schema allows.

        Must handle failures (unreachable URL, unreadable image, timeout) per
        item — never raise for a single bad item, return an `ItemPrediction`
        with empty shapes and `error` set instead.
        """
        ...

    async def interactive(self, request: InteractiveRequest) -> InteractiveResponse:
        """Turn a point/box/text prompt into a polygon. Must return in well under 1s."""
        ...

    async def embed(self, items: list[EmbedItem]) -> list[ItemEmbedding]:
        """Compute a deterministic fixed-length embedding vector per item."""
        ...
