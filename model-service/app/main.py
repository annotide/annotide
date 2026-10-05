"""Reference model service implementing the platform's §8 model interface.

One HTTP contract covers every way a customer supplies a model (BYOM): a
self-hosted endpoint, an Azure ML or SageMaker deployment, or a container like
this one. Swap the backend, keep the contract, and the platform needs no
changes.

    GET  /health       liveness
    GET  /ready        readiness, names the loaded backend
    GET  /info         model identity, classes, media types
    POST /predict      batch pre-labelling (ML-2)
    POST /interactive  point/box prompt -> polygon, sub-second (ML-7)
    POST /embed        vectors for search and clustering (ML-12)
    POST /ocr          words of scanned pages (Tesseract or an external vision LLM)

With `MODEL_API_KEY` set, every route except `/health` and `/ready` (the probes)
requires `Authorization: Bearer <key>`. Unset, the service is open, as before.

The service never writes to the platform. It answers with annotations and the
platform decides what to store, which is what keeps a model swappable.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from PIL import Image

from app import ocr
from app.backends import get_backend
from app.backends.base import ModelBackend
from app.backends.heuristic import fetch_image_bytes
from app.schemas import (
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    InfoResponse,
    InteractiveRequest,
    InteractiveResponse,
    MediaType,
    OcrPageOut,
    OcrRequest,
    OcrResponse,
    OcrWordOut,
    PredictRequest,
    PredictResponse,
    ReadyResponse,
)

SERVICE_PORT = int(os.environ.get("PORT", "9000"))

#: Refuse an unbounded batch rather than running out of memory mid-request.
#: The platform pages through a large project; a single call is a page.
MAX_PREDICT_ITEMS = 256
MAX_EMBED_ITEMS = 512

# Starlette renamed these constants and deprecated the old names; the numbers
# are stable across every version.
HTTP_413_TOO_LARGE = 413
HTTP_422_UNPROCESSABLE = 422
HTTP_501_NOT_IMPLEMENTED = 501
HTTP_502_BAD_GATEWAY = 502

_backend: ModelBackend | None = None


def backend() -> ModelBackend:
    """The loaded backend. Raises 503 rather than 500 when it failed to load."""
    if _backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The model backend is not loaded.",
        )
    return _backend


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the backend once, at start-up.

    Loading on the first request instead would make that request pay for model
    initialisation and would hide a bad `MODEL_PATH` until someone tried to use
    it. Failing here means the container never reports healthy.
    """
    global _backend
    _backend = get_backend()
    # A bad OCR configuration (an unknown engine, a missing URL) fails here too.
    ocr.get_engine()
    yield
    _backend = None


app = FastAPI(
    title="Annotide — reference model service",
    version="0.1.0",
    summary="A worked implementation of the platform's model interface (§8)",
    lifespan=lifespan,
)


#: Open without the key even when `MODEL_API_KEY` is set: orchestrator probes.
_PUBLIC_PATHS = frozenset({"/health", "/ready"})


@app.middleware("http")
async def _require_api_key(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Optional bearer key (`MODEL_API_KEY`). Read per request, so a rotation needs no restart."""
    key = os.environ.get("MODEL_API_KEY", "")
    if key and request.url.path not in _PUBLIC_PATHS:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), key.encode()
        ):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing or invalid API key."},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
    # A backend rejecting a prompt or a schema is the caller's mistake, not a
    # server fault; 400 tells the platform not to retry.
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    """Always 200 while the process is alive. Consults nothing."""
    return HealthResponse()


@app.get("/ready", response_model=ReadyResponse, summary="Readiness")
async def ready() -> ReadyResponse:
    """200 once a backend is loaded and able to serve predictions."""
    loaded = backend()
    return ReadyResponse(backend=loaded.name)


@app.get("/info", response_model=InfoResponse, summary="Model identity and capabilities")
async def info() -> InfoResponse:
    """What this model is and what it can produce.

    The platform reads this when registering the model, so its class list can
    be mapped onto a project's label schema (BYOM-2).
    """
    loaded = backend()
    return InfoResponse(
        name=loaded.name,
        version=loaded.version,
        media_types=loaded.media_types,
        classes=loaded.classes(),
        gpu=loaded.gpu,
        backend=loaded.name,
        ocr_engine=engine.name if (engine := ocr.get_engine()) else None,
    )


@app.post("/predict", response_model=PredictResponse, summary="Batch pre-labelling")
async def predict(payload: PredictRequest) -> PredictResponse:
    """Pre-label a batch of items (ML-2).

    Every shape returned is in ORIGINAL IMAGE PIXELS and carries a confidence,
    so the platform can hide or highlight low-confidence output (ML-3). A model
    may only emit classes present in the posted schema — introducing a class a
    project has not defined would corrupt its label set.

    One unreadable item yields empty shapes and an `error` for that item; it
    never fails the batch, because a single corrupt file in a million-object
    container must not block pre-labelling the rest.
    """
    if not payload.items:
        return PredictResponse(predictions=[])
    if len(payload.items) > MAX_PREDICT_ITEMS:
        raise HTTPException(
            status_code=HTTP_413_TOO_LARGE,
            detail=f"At most {MAX_PREDICT_ITEMS} items per request.",
        )

    predictions = await backend().predict(
        payload.items, payload.schema_, payload.confidence_threshold
    )
    return PredictResponse(predictions=predictions)


@app.post(
    "/interactive",
    response_model=InteractiveResponse,
    summary="Prompt-driven segmentation",
)
async def interactive(payload: InteractiveRequest) -> InteractiveResponse:
    """Turn a click or a box into a polygon (ML-7).

    This sits in the annotator's inner loop — a person clicks and waits — so it
    must answer in well under a second. A slow implementation here is worse
    than none at all.
    """
    if payload.point is None and payload.box is None and not payload.text:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail="Provide one of 'point', 'box' or 'text'.",
        )
    return await backend().interactive(payload)


@app.post("/embed", response_model=EmbedResponse, summary="Embedding vectors")
async def embed(payload: EmbedRequest) -> EmbedResponse:
    """Vectors for similarity search, clustering and duplicate detection (ML-12)."""
    if not payload.items:
        return EmbedResponse(dimensions=0, embeddings=[])
    if len(payload.items) > MAX_EMBED_ITEMS:
        raise HTTPException(
            status_code=HTTP_413_TOO_LARGE,
            detail=f"At most {MAX_EMBED_ITEMS} items per request.",
        )

    embeddings = await backend().embed(payload.items)
    dimensions = len(embeddings[0].vector) if embeddings else 0
    return EmbedResponse(dimensions=dimensions, embeddings=embeddings)


@app.exception_handler(ocr.OcrError)
async def _ocr_error(_: Request, exc: ocr.OcrError) -> JSONResponse:
    # The engine (a subprocess or an upstream LLM) failed: a gateway error.
    return JSONResponse(status_code=HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})


@app.post("/ocr", response_model=OcrResponse, summary="Words of scanned pages")
async def read_text(payload: OcrRequest) -> OcrResponse:
    """Read the words of a scanned PDF's pages, or of an image.

    For pdf the boxes are in each page's points (top-left origin, `/Rotate`
    applied) — the space pdf shapes use; for an image, in its pixels. The
    engine is the service's `OCR_ENGINE`; 501 when OCR is off.
    """
    engine = ocr.get_engine()
    if engine is None:
        raise HTTPException(
            status_code=HTTP_501_NOT_IMPLEMENTED,
            detail="No OCR engine is configured (OCR_ENGINE).",
        )
    limit = ocr.max_pages()
    if payload.pages is not None and len(payload.pages) > limit:
        raise HTTPException(
            status_code=HTTP_413_TOO_LARGE, detail=f"At most {limit} pages per request."
        )
    try:
        data = await fetch_image_bytes(payload.url)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ValueError(f"could not fetch the item: {exc}") from exc

    if payload.media_type is MediaType.PDF:
        pages = payload.pages
        if pages is None:
            count = await asyncio.to_thread(ocr.page_count_or_error, data)
            pages = list(range(1, min(count, limit) + 1))
        read = await ocr.ocr_pdf(data, sorted(set(pages)), engine)
    elif payload.media_type is MediaType.IMAGE:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f"could not decode the image: {exc}") from exc
        read = [await ocr.ocr_image(image, engine)]
    else:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE, detail="OCR reads pdf and image items."
        )
    return OcrResponse(
        engine=engine.name,
        pages=[
            OcrPageOut(
                page=page.page,
                width=page.width,
                height=page.height,
                words=[
                    OcrWordOut(text=w.text, bbox=w.bbox, confidence=w.confidence)
                    for w in page.words
                ],
            )
            for page in read
        ],
    )


def _main() -> Any:  # pragma: no cover - entry point
    import uvicorn

    return uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)


if __name__ == "__main__":  # pragma: no cover
    _main()
