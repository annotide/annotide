"""The default, offline `ModelBackend`.

**This is a stand-in for a real detector, not a useful model.** It has no
learned weights and makes no claim to recognise objects. It exists so
`docker compose up` produces a working model-assisted pre-labeling demo with
no GPU, no downloaded weights and no network access to a model host — useful
for trying the platform's review workflow and for showing correction-rate
metrics (ML-5) on *something*, and as the simplest possible example of the
`ModelBackend` protocol.

Detection method: fetch the image, downscale it to a fixed resolution,
compute a Sobel-like edge-energy map (gradient magnitude of luminance),
threshold it, find connected components, keep the largest ones above a
minimum area, score each by its mean normalised energy, and merge
heavily-overlapping boxes with NMS. It reliably draws boxes around
high-contrast regions (edges, text, textured objects) — not around
"cars" or "people" as such.

Every step here is pure, seeded by nothing but the input image bytes, so the
same image always produces the same boxes, scores and shape ids (ids are
derived via `uuid.uuid5`, not `uuid4`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import socket
import uuid
from typing import Final
from urllib.parse import urlsplit

import httpx
import numpy as np
from PIL import Image

from app import ocr
from app.backends import doc_fields, text_ner
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
    SpanShape,
    ToolType,
)

_NAMESPACE: Final = uuid.UUID("2c9c9f7a-3b1a-4f0e-9c1a-7a2b6d4e5f60")

MAX_IMAGE_BYTES: Final = 20 * 1024 * 1024  # 20 MB cap on fetched images
FETCH_TIMEOUT: Final = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)

#: Cloud instance-metadata endpoints. Media normally comes from the platform at
#: a private address, so private ranges stay allowed; these never hold media and
#: answer with credentials, so a caller-supplied URL may not reach them.
_METADATA_HOSTS: Final = frozenset({"metadata.google.internal", "metadata.goog", "metadata"})
_METADATA_ADDRESSES: Final = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local: AWS, Azure, GCP, OCI metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud
)

DOWNSCALE_MAX_DIM: Final = 256
ENERGY_THRESHOLD: Final = 0.35
MIN_AREA_FRACTION: Final = 0.002  # of the downscaled image area
MAX_DETECTIONS: Final = 5
IOU_THRESHOLD: Final = 0.5

EMBED_SIDE: Final = 8  # 8x8 -> 64-dim embedding
EMBED_DIM: Final = EMBED_SIDE * EMBED_SIDE

POINT_PROMPT_RADIUS: Final = 24.0
TEXT_PROMPT_BOX_FRACTION: Final = 0.2

Box = tuple[float, float, float, float]


async def _with_ocr_words(
    data: bytes, words: list[doc_fields.Word], engine: ocr.OcrEngine
) -> list[doc_fields.Word]:
    """`words` plus OCR'd words of the pages that have none, in page order."""
    count = await asyncio.to_thread(ocr.page_count_or_error, data)
    with_text = {word.page for word in words}
    scans = [n for n in range(1, count + 1) if n not in with_text][: ocr.max_pages()]
    if not scans:
        return words
    read = await ocr.ocr_pdf(data, scans, engine)
    extra = [
        doc_fields.Word(page=page.page, text=word.text, bbox=word.bbox)
        for page in read
        for word in page.words
    ]
    # Stable: each page's words keep their reading order.
    return sorted([*words, *extra], key=lambda word: word.page)


def _pdf_spans(
    item_id: str, words: list[doc_fields.Word], span_classes: set[str], threshold: float
) -> list[SpanShape]:
    """NER spans over each page's words, one box per line (CONTRACTS.md "PDF items")."""
    shapes: list[SpanShape] = []
    pages = sorted({word.page for word in words})
    for page in pages:
        on_page = [word for word in words if word.page == page]
        text, ranges = doc_fields.page_text(on_page)
        for entity in text_ner.find_entities(text):
            if entity.label not in span_classes or entity.score < threshold:
                continue
            covered = [
                on_page[i] for i in doc_fields.words_in_range(ranges, entity.start, entity.end)
            ]
            boxes = [
                (max(0.0, x0), max(0.0, y0), x1, y1)
                for x0, y0, x1, y1 in doc_fields.line_boxes(covered)
                if x1 > x0 and y1 > y0
            ][:256]
            if not boxes:
                continue
            shapes.append(
                SpanShape(
                    id=uuid.uuid5(_NAMESPACE, f"{item_id}:{page}:{entity.start}:{entity.end}"),
                    class_=entity.label,
                    confidence=entity.score,
                    page=page,
                    boxes=boxes,
                    text=" ".join(word.text for word in covered),
                )
            )
    return shapes


async def fetch_image_bytes(url: str) -> bytes:
    """Fetch raw image bytes from `url`, with a timeout and a size cap.

    Supports `data:` URLs (decoded locally, no network) and `http(s)://` URLs
    (streamed via httpx, aborted once `MAX_IMAGE_BYTES` is exceeded).
    """
    if url.startswith("data:"):
        header, _, data_part = url.partition(",")
        if ";base64" not in header:
            raise ValueError("data URL must be base64-encoded")
        raw = base64.b64decode(data_part)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} byte cap")
        return raw

    await refuse_metadata_url(url)
    async with (
        httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} byte cap")
        return bytes(chunks)


async def refuse_metadata_url(url: str) -> None:
    """Raise `ValueError` unless `url` is http(s) and resolves to no metadata address.

    Redirects are not followed, so the address checked is the one fetched,
    short of DNS changing between the two lookups.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("media URL must be http(s) with a host")
    if host in _METADATA_HOSTS:
        raise ValueError("media URL points at a cloud metadata endpoint")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parts.port or 443, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise ValueError(f"cannot resolve media host {host!r}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if any(address in network for network in _METADATA_ADDRESSES):
            raise ValueError("media URL points at a cloud metadata endpoint")


def load_image(raw: bytes) -> Image.Image:
    """Decode raw bytes into an RGB `PIL.Image`."""
    import io

    image = Image.open(io.BytesIO(raw))
    image.load()
    return image.convert("RGB")


def iou(a: Box, b: Box) -> float:
    """Intersection-over-union of two `[x_min, y_min, x_max, y_max]` boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def nms(boxes: list[Box], scores: list[float], iou_threshold: float = IOU_THRESHOLD) -> list[int]:
    """Greedy non-max suppression.

    Returns the indices of kept boxes, highest score first. Ties in score are
    broken by box position so the result is deterministic regardless of input
    order.
    """
    order = sorted(range(len(boxes)), key=lambda i: (-scores[i], boxes[i][0], boxes[i][1]))
    suppressed = [False] * len(boxes)
    keep: list[int] = []
    for i in order:
        if suppressed[i]:
            continue
        keep.append(i)
        for j in order:
            if j != i and not suppressed[j] and iou(boxes[i], boxes[j]) > iou_threshold:
                suppressed[j] = True
    return keep


def _energy_map(image: Image.Image) -> tuple[np.ndarray, float]:
    """Downscale `image` and compute a normalised (0..1) edge-energy map.

    Returns the energy map and the scale factor applied (multiply an original
    pixel coordinate by this to get its position in the map).
    """
    width, height = image.size
    scale = min(1.0, DOWNSCALE_MAX_DIM / max(width, height))
    small_w = max(1, round(width * scale))
    small_h = max(1, round(height * scale))
    small = image.resize((small_w, small_h), Image.Resampling.BILINEAR)
    gray = np.asarray(small.convert("L"), dtype=np.float64)
    grad_y, grad_x = np.gradient(gray)
    energy = np.sqrt(grad_x**2 + grad_y**2)
    max_energy = float(energy.max())
    if max_energy > 0:
        energy = energy / max_energy
    return energy, scale


def _connected_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """Label 4-connected `True` regions of `mask`; return one coordinate list per region."""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[list[tuple[int, int]]] = []
    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or visited[start_y, start_x]:
                continue
            visited[start_y, start_x] = True
            stack = [(start_y, start_x)]
            coords: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                coords.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < height
                        and 0 <= nx < width
                        and mask[ny, nx]
                        and not visited[ny, nx]
                    ):
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            components.append(coords)
    return components


def detect_boxes(image: Image.Image) -> list[tuple[Box, float]]:
    """Detect up to `MAX_DETECTIONS` image-derived boxes with a 0..1 confidence each.

    Deterministic for identical image bytes: same downscale, same energy map,
    same component labeling order, same NMS tie-breaks.
    """
    width, height = image.size
    energy, scale = _energy_map(image)
    mask = energy > ENERGY_THRESHOLD
    small_h, small_w = mask.shape
    min_area = MIN_AREA_FRACTION * small_w * small_h

    candidates: list[tuple[Box, float]] = []
    for coords in _connected_components(mask):
        if len(coords) < min_area:
            continue
        ys = [c[0] for c in coords]
        xs = [c[1] for c in coords]
        y_min, y_max = min(ys), max(ys)
        x_min, x_max = min(xs), max(xs)
        comp_energy = float(np.mean([energy[y, x] for y, x in coords]))

        ox1 = max(0.0, min(x_min / scale, float(width)))
        oy1 = max(0.0, min(y_min / scale, float(height)))
        ox2 = max(0.0, min((x_max + 1) / scale, float(width)))
        oy2 = max(0.0, min((y_max + 1) / scale, float(height)))
        if ox2 <= ox1 or oy2 <= oy1:
            continue
        candidates.append(((ox1, oy1, ox2, oy2), comp_energy))

    if not candidates:
        return []

    candidates.sort(key=lambda c: (-c[1], c[0][0], c[0][1]))
    boxes = [c[0] for c in candidates]
    scores = [c[1] for c in candidates]
    kept_idx = nms(boxes, scores, IOU_THRESHOLD)
    kept = [(boxes[i], scores[i]) for i in kept_idx]
    kept.sort(key=lambda c: (-c[1], c[0][0], c[0][1]))
    return kept[:MAX_DETECTIONS]


class HeuristicBackend:
    """Offline, weight-free edge-energy detector. See module docstring."""

    name: str
    version: str
    gpu: bool
    media_types: list[MediaType]

    def __init__(self) -> None:
        # Instance attributes, not class attributes: the ModelBackend Protocol
        # declares them as instance variables, and a mutable list shared across
        # instances is a hazard regardless.
        self.name = "heuristic-edge-energy"
        self.version = "0.1.0"
        self.gpu = False
        self.media_types = [MediaType.IMAGE, MediaType.TEXT, MediaType.PDF]

    def classes(self) -> list[str]:
        """One generic image class, `object`, the text NER labels and the PDF field labels."""
        return ["object", *text_ner.LABELS, *doc_fields.LABELS]

    def _usable_classes(self, schema: LabelSchemaDefinition) -> list[str]:
        return sorted(
            cls.name
            for cls in schema.classes
            if cls.name in self.classes() and ToolType.BBOX in cls.tools
        )

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        confidence_threshold: float,
    ) -> list[ItemPrediction]:
        usable_classes = self._usable_classes(schema)
        span_classes = {
            cls.name
            for cls in schema.classes
            if cls.name in text_ner.LABELS and ToolType.SPAN in cls.tools
        }
        predictions: list[ItemPrediction] = []
        for item in items:
            if item.media_type is MediaType.TEXT:
                predictions.append(
                    await self._predict_text(item, schema, span_classes, confidence_threshold)
                )
                continue
            if item.media_type is MediaType.PDF:
                predictions.append(
                    await self._predict_pdf(
                        item, schema, usable_classes, span_classes, confidence_threshold
                    )
                )
                continue
            predictions.append(
                await self._predict_one(item, schema, usable_classes, confidence_threshold)
            )
        return predictions

    async def _predict_pdf(
        self,
        item: PredictItem,
        schema: LabelSchemaDefinition,
        usable_classes: list[str],
        span_classes: set[str],
        confidence_threshold: float,
    ) -> ItemPrediction:
        """Amounts, dates and e-mails (bboxes) and PER/ORG/LOC spans from the text layer.

        Spans: `text_ner` runs over each page's words joined like the platform
        export (`doc_fields.page_text`); an entity becomes one box per line.
        OCR'd words carry no line-break info and are joined by spaces; lines
        are still split geometrically when boxes are built.

        Pages without a text layer (scans) are read by the OCR engine first,
        when one is configured, up to `OCR_MAX_PAGES` of them.
        """
        empty = AnnotationResult(schema_version=schema.version, media_type=MediaType.PDF)
        wanted = set(usable_classes) & set(doc_fields.LABELS)
        if not wanted and not span_classes:
            return ItemPrediction(item_id=item.id, result=empty, confidence=0.0)
        try:
            data = await fetch_image_bytes(item.url)
            words = await asyncio.to_thread(doc_fields.pdf_words, data)
        except Exception as exc:
            return ItemPrediction(
                item_id=item.id, result=empty, confidence=0.0, error=f"could not read pdf: {exc}"
            )
        engine = ocr.get_engine()
        if engine is not None:
            try:
                words = await _with_ocr_words(data, words, engine)
            except (ValueError, ocr.OcrError) as exc:
                return ItemPrediction(
                    item_id=item.id, result=empty, confidence=0.0, error=f"ocr failed: {exc}"
                )
        shapes: list[BBoxShape | SpanShape] = []
        for found in doc_fields.find_fields(words):
            if found.label not in wanted or found.score < confidence_threshold:
                continue
            x0, y0, x1, y1 = found.bbox
            if x1 <= x0 or y1 <= y0:
                continue
            shapes.append(
                BBoxShape(
                    id=uuid.uuid5(_NAMESPACE, f"{item.id}:{found.page}:{found.bbox}"),
                    class_=found.label,
                    confidence=found.score,
                    page=found.page,
                    bbox=(max(0.0, x0), max(0.0, y0), x1, y1),
                    text=found.text,
                )
            )
        if span_classes:
            shapes.extend(_pdf_spans(item.id, words, span_classes, confidence_threshold))
        confidence = (
            round(sum(s.confidence or 0.0 for s in shapes) / len(shapes), 4) if shapes else 0.0
        )
        result = AnnotationResult(
            schema_version=schema.version, media_type=MediaType.PDF, shapes=shapes
        )
        return ItemPrediction(item_id=item.id, result=result, confidence=confidence)

    async def _predict_text(
        self,
        item: PredictItem,
        schema: LabelSchemaDefinition,
        span_classes: set[str],
        confidence_threshold: float,
    ) -> ItemPrediction:
        """Capitalisation-rule NER over a text item (see `text_ner`)."""
        empty = AnnotationResult(schema_version=schema.version, media_type=MediaType.TEXT)
        if not span_classes:
            return ItemPrediction(item_id=item.id, result=empty, confidence=0.0)
        try:
            text = (await fetch_image_bytes(item.url)).decode("utf-8", errors="replace")
        except Exception as exc:
            return ItemPrediction(
                item_id=item.id, result=empty, confidence=0.0, error=f"could not load text: {exc}"
            )
        shapes: list[SpanShape] = []
        for entity in text_ner.find_entities(text):
            if entity.label not in span_classes or entity.score < confidence_threshold:
                continue
            shapes.append(
                SpanShape(
                    id=uuid.uuid5(_NAMESPACE, f"{item.id}:{entity.start}:{entity.end}"),
                    class_=entity.label,
                    confidence=entity.score,
                    start=entity.start,
                    end=entity.end,
                    text=text[entity.start : entity.end],
                )
            )
        confidence = (
            round(sum(s.confidence or 0.0 for s in shapes) / len(shapes), 4) if shapes else 0.0
        )
        result = AnnotationResult(
            schema_version=schema.version, media_type=MediaType.TEXT, shapes=shapes
        )
        return ItemPrediction(item_id=item.id, result=result, confidence=confidence)

    async def _predict_one(
        self,
        item: PredictItem,
        schema: LabelSchemaDefinition,
        usable_classes: list[str],
        confidence_threshold: float,
    ) -> ItemPrediction:
        empty = AnnotationResult(
            schema_version=schema.version, media_type=MediaType.IMAGE, shapes=[]
        )
        if not usable_classes:
            # The schema has no class this model can emit — zero shapes, no invented class.
            return ItemPrediction(item_id=item.id, result=empty, confidence=0.0)

        try:
            raw = await fetch_image_bytes(item.url)
            image = load_image(raw)
        except Exception as exc:
            return ItemPrediction(
                item_id=item.id,
                result=empty,
                confidence=0.0,
                error=f"could not load image: {exc}",
            )

        class_name = usable_classes[0]
        shapes: list[BBoxShape] = []
        for index, (box, score) in enumerate(detect_boxes(image)):
            confidence = round(min(1.0, max(0.0, score)), 4)
            if confidence < confidence_threshold:
                continue
            shape_id = uuid.uuid5(
                _NAMESPACE,
                f"{item.id}:{index}:{box[0]:.2f}:{box[1]:.2f}:{box[2]:.2f}:{box[3]:.2f}:{class_name}",
            )
            shapes.append(
                BBoxShape(id=shape_id, class_=class_name, confidence=confidence, bbox=box)
            )

        item_confidence = (
            round(sum(s.confidence or 0.0 for s in shapes) / len(shapes), 4) if shapes else 0.0
        )
        result = AnnotationResult(
            schema_version=schema.version, media_type=MediaType.IMAGE, shapes=shapes
        )
        return ItemPrediction(item_id=item.id, result=result, confidence=item_confidence)

    async def interactive(self, request: InteractiveRequest) -> InteractiveResponse:
        width, height = float(request.width), float(request.height)

        if request.box is not None:
            x_min, y_min, x_max, y_max = request.box.bbox
        elif request.point is not None:
            x_min = request.point.x - POINT_PROMPT_RADIUS
            y_min = request.point.y - POINT_PROMPT_RADIUS
            x_max = request.point.x + POINT_PROMPT_RADIUS
            y_max = request.point.y + POINT_PROMPT_RADIUS
        elif request.text is not None:
            # No grounding model available offline: derive a deterministic,
            # repeatable placeholder region from the text itself so the same
            # prompt always yields the same span. Not a real grounding result.
            digest = hashlib.sha256(request.text.encode()).digest()
            cx = width * (0.2 + 0.6 * digest[0] / 255.0)
            cy = height * (0.2 + 0.6 * digest[1] / 255.0)
            box_w = width * TEXT_PROMPT_BOX_FRACTION
            box_h = height * TEXT_PROMPT_BOX_FRACTION
            x_min, y_min = cx - box_w / 2, cy - box_h / 2
            x_max, y_max = cx + box_w / 2, cy + box_h / 2
        else:
            raise ValueError("interactive prompt requires one of point, box, or text")

        x_min = max(0.0, min(x_min, width))
        y_min = max(0.0, min(y_min, height))
        x_max = max(0.0, min(x_max, width))
        y_max = max(0.0, min(y_max, height))
        if x_max <= x_min:
            x_max = min(width, x_min + 1.0)
        if y_max <= y_min:
            y_max = min(height, y_min + 1.0)

        points = [(x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)]
        return InteractiveResponse(points=points, confidence=0.5)

    async def embed(self, items: list[EmbedItem]) -> list[ItemEmbedding]:
        embeddings: list[ItemEmbedding] = []
        for item in items:
            try:
                raw = await fetch_image_bytes(item.url)
                image = load_image(raw)
            except Exception as exc:
                embeddings.append(
                    ItemEmbedding(
                        item_id=item.id,
                        vector=[0.0] * EMBED_DIM,
                        error=f"could not load image: {exc}",
                    )
                )
                continue

            small = image.convert("L").resize((EMBED_SIDE, EMBED_SIDE), Image.Resampling.BILINEAR)
            arr = np.asarray(small, dtype=np.float64) / 255.0
            vector = [round(float(v) * 2 - 1, 6) for v in arr.flatten()]
            embeddings.append(ItemEmbedding(item_id=item.id, vector=vector))
        return embeddings
