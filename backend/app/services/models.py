"""Model gateway: class mapping and the HTTP client for a registered model (§8).

Two halves, kept apart so the first is testable without a network:

- **Class mapping (BYOM-2).** A customer's model speaks its own class names
  (`person`, `vehicle`), a project's schema speaks the project's
  (`pedestrian`, `car`). `model_version.class_mapping` translates between
  them, in both directions: :func:`model_facing_schema` builds the schema the
  model is *posted* (its own names, so it can emit them) and
  :func:`map_result` rewrites what comes *back* into schema names, dropping
  what the project has no class for. Nothing else on the platform ever sees a
  model class name.
- **The client (BYOM-3).** :class:`ModelClient` talks the §8 contract
  (`/info`, `/predict`) to `model.endpoint_url` with the credential resolved
  from `model.secret_ref`, or with a Microsoft Entra token for a managed
  identity or service principal, fetched per request so that a job running
  for hours never sends an expired one. Errors are split into :class:`ModelUnavailable`
  (transient: the worker retries) and :class:`ModelRejected` (permanent: a
  4xx, the job fails).

The reference implementation of the other side is `model-service/`.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import AsyncGenerator, Generator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

import httpx
from azure.core.exceptions import ClientAuthenticationError

from app.schemas import AnnotationResult, ClassDef, LabelSchemaDefinition, tool_for_shape
from app.services.secrets import resolve_secret

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

#: Upper bound on one `/predict` call, matching `model-service`'s own limit.
MAX_PREDICT_ITEMS = 256

#: `/predict` can take a while on CPU; `/info` should not.
_PREDICT_TIMEOUT_S = 300.0
_INFO_TIMEOUT_S = 10.0
# A person is waiting on a click (ML-7); anything slower than this is broken.
_INTERACTIVE_TIMEOUT_S = 8.0
#: An external vision LLM can take a minute on a dense page.
_OCR_TIMEOUT_S = 120.0
#: Words kept per page; a page has far fewer, a broken model does not get unbounded.
MAX_OCR_WORDS = 5000


class ModelIdentity(enum.StrEnum):
    """How the platform authenticates to a model endpoint (BYOM-3)."""

    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    SERVICE_PRINCIPAL = "service_principal"
    MANAGED_IDENTITY = "managed_identity"


#: Identities that sign in to Microsoft Entra for a token instead of sending a
#: stored secret; their non-secret half is `model.identity_config`.
ENTRA_IDENTITIES = frozenset({ModelIdentity.SERVICE_PRINCIPAL, ModelIdentity.MANAGED_IDENTITY})


def identity_problem(
    identity_type: str, secret_ref: str | None, identity_config: Mapping[str, Any] | None
) -> str | None:
    """Why this identity cannot work as configured, or None when it can (BYOM-3)."""
    identity = ModelIdentity(identity_type)
    config = identity_config or {}
    if identity is ModelIdentity.NONE:
        return None
    if identity is ModelIdentity.MANAGED_IDENTITY:
        if secret_ref:
            return "identity_type 'managed_identity' takes no secret_ref."
    elif not secret_ref:
        return f"identity_type {identity.value!r} requires a secret_ref."
    if identity not in ENTRA_IDENTITIES:
        return None
    required = ["scope"]
    if identity is ModelIdentity.SERVICE_PRINCIPAL:
        required += ["tenant_id", "client_id"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        return f"identity_type {identity.value!r} requires identity_config.{', '.join(missing)}."
    return None


class ModelError(Exception):
    """Base for failures talking to a model endpoint."""


class ModelUnavailable(ModelError):  # noqa: N818 - a state, not an "…Error"
    """The endpoint could not be reached or answered 5xx. Transient: retry."""


class ModelRejected(ModelError):  # noqa: N818 - a state, not an "…Error"
    """The endpoint answered 4xx: wrong credentials, bad request. Permanent."""


# --------------------------------------------------------------------------- #
# Class mapping (BYOM-2) — pure functions
# --------------------------------------------------------------------------- #


def normalise_mapping(raw: dict[str, Any]) -> dict[str, str | None]:
    """Validate a stored `class_mapping`: string keys, string-or-null values."""
    mapping: dict[str, str | None] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"class_mapping keys must be non-empty strings, got {key!r}")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"class_mapping[{key!r}] must be a string or null, got {value!r}")
        mapping[key] = value
    return mapping


def validate_mapping(
    mapping: dict[str, str | None], definition: LabelSchemaDefinition
) -> list[str]:
    """Names on the right-hand side of the mapping that the schema does not define."""
    known = {c.name for c in definition.classes}
    return sorted(
        {target for target in mapping.values() if target is not None and target not in known}
    )


def model_facing_schema(
    definition: LabelSchemaDefinition, mapping: dict[str, str | None]
) -> LabelSchemaDefinition:
    """The schema posted to the model: the model's own class names (BYOM-2).

    Each mapped model class becomes a class carrying the *target* class's
    tools and attributes, so the model still learns which shape types the
    project accepts. Model classes mapped to `null` are omitted (the model
    need not bother). With an empty mapping the project schema is sent as is.
    """
    if not mapping:
        return definition

    by_name = {c.name: c for c in definition.classes}
    classes: list[ClassDef] = []
    for model_class, target in mapping.items():
        if target is None:
            continue
        template = by_name.get(target)
        if template is None:
            # validate_mapping() reports this; here we simply cannot build it.
            continue
        classes.append(
            template.model_copy(update={"name": model_class, "display_name": model_class})
        )
    return definition.model_copy(update={"classes": classes})


def map_result(
    result: AnnotationResult,
    mapping: dict[str, str | None],
    definition: LabelSchemaDefinition,
) -> tuple[AnnotationResult, int]:
    """Rewrite a prediction's class names into schema names (BYOM-2).

    Returns the mapped result and how many shapes were dropped: a shape is
    dropped when its class maps to `null`, is unmapped and the schema has no
    class of that name, or lands on a class that does not allow the shape's
    tool (a bbox mapped onto a polygon-only class). Model output is stored as
    an unvalidated draft, so this is the one check between the model and the
    database. Classification values pass through untouched (they are keyed
    by the schema's own field names).
    """
    classes = {c.name: c for c in definition.classes}
    kept = []
    dropped = 0
    for shape in result.shapes:
        # Explicitly mapped wins; otherwise keep only what the schema names.
        fallback = shape.class_ if shape.class_ in classes else None
        target = mapping.get(shape.class_, fallback)
        class_def = classes.get(target) if target is not None else None
        if class_def is None or tool_for_shape(shape) not in class_def.tools:
            dropped += 1
            continue
        kept.append(shape.model_copy(update={"class_": target}))
    return result.model_copy(update={"shapes": kept}), dropped


# --------------------------------------------------------------------------- #
# HTTP client (BYOM-3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PredictItem:
    """One item handed to `/predict`: the id we want back, a URL the model can fetch.

    `media_type` is `image`, `text` or `pdf`; text and pdf items have `width` /
    `height` 0 (a pdf's page sizes are the model's to read, in points).
    """

    id: str
    url: str
    width: int
    height: int
    media_type: str = "image"


@dataclass(frozen=True, slots=True)
class InteractivePolygon:
    """What `/interactive` answered: a polygon in image pixels (ML-7)."""

    points: list[tuple[float, float]]
    confidence: float


@dataclass(frozen=True, slots=True)
class OcrPage:
    """What `/ocr` answered for one page: words in page points."""

    page: int
    width: float
    height: float
    engine: str
    words: list[tuple[str, tuple[float, float, float, float]]]


@dataclass(frozen=True, slots=True)
class Prediction:
    """One entry of a `/predict` response."""

    item_id: str
    result: AnnotationResult
    confidence: float
    error: str | None


async def auth_headers(identity_type: str, secret_ref: str | None) -> dict[str, str]:
    """Headers carrying the model's stored credential (BYOM-3); empty for `none`.

    Entra identities have no fixed header: see :class:`EntraAuth`.
    """
    identity = ModelIdentity(identity_type)
    if identity is ModelIdentity.NONE:
        return {}
    if identity in ENTRA_IDENTITIES:
        raise ValueError(f"identity_type {identity.value!r} signs in per request (EntraAuth)")
    secret = await resolve_secret(secret_ref)
    if not secret:
        raise ModelRejected(f"identity_type {identity.value!r} needs a secret_ref with a value")
    if identity is ModelIdentity.API_KEY:
        return {"X-API-Key": secret}
    return {"Authorization": f"Bearer {secret}"}


class EntraAuth(httpx.Auth):
    """`Authorization: Bearer <Entra token>` on every request (BYOM-3).

    The token is asked for per request, not once: the credential caches it
    and renews it five minutes before it expires, so a prelabel job that holds
    one client for hours keeps sending a valid token. The credential is owned
    here and closed with the client.
    """

    def __init__(self, credential: AsyncTokenCredential, scope: str) -> None:
        self._credential = credential
        self._scope = scope

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        raise RuntimeError("EntraAuth is async-only")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        try:
            token = await self._credential.get_token(self._scope)
        except ClientAuthenticationError as exc:
            # No identity on this host, a wrong secret or tenant: permanent.
            raise ModelRejected(f"Entra sign-in failed: {exc.message}") from exc
        except Exception as exc:  # network errors from azure-core / aiohttp
            raise ModelUnavailable(f"could not reach Entra: {type(exc).__name__}") from exc
        request.headers["Authorization"] = f"Bearer {token.token}"
        yield request

    async def aclose(self) -> None:
        # Shutdown must not fail because the session was already closed.
        with contextlib.suppress(Exception):
            await self._credential.close()


async def entra_auth(
    identity_type: str, secret_ref: str | None, identity_config: Mapping[str, Any] | None
) -> EntraAuth:
    """An :class:`EntraAuth` for a `managed_identity` or `service_principal` model."""
    identity = ModelIdentity(identity_type)
    problem = identity_problem(identity_type, secret_ref, identity_config)
    if problem:
        raise ModelRejected(problem)
    config = identity_config or {}
    credential: AsyncTokenCredential
    if identity is ModelIdentity.MANAGED_IDENTITY:
        from azure.identity.aio import ManagedIdentityCredential

        # No client id: the system-assigned identity, or on AKS the workload
        # identity named by AZURE_CLIENT_ID.
        credential = ManagedIdentityCredential(client_id=config.get("client_id") or None)
    else:
        from azure.identity.aio import ClientSecretCredential

        secret = await resolve_secret(secret_ref)
        if not secret:
            raise ModelRejected("identity_type 'service_principal' needs a secret_ref with a value")
        credential = ClientSecretCredential(
            tenant_id=str(config["tenant_id"]),
            client_id=str(config["client_id"]),
            client_secret=secret,
        )
    return EntraAuth(credential, str(config["scope"]))


class ModelClient:
    """Async client for one model endpoint. Use as an async context manager."""

    def __init__(
        self, endpoint_url: str, headers: dict[str, str], auth: EntraAuth | None = None
    ) -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(
            base_url=endpoint_url.rstrip("/"),
            headers={"Accept": "application/json", **headers},
            auth=auth,
        )

    @classmethod
    async def for_model(
        cls,
        endpoint_url: str | None,
        identity_type: str,
        secret_ref: str | None,
        identity_config: Mapping[str, Any] | None = None,
    ) -> Self:
        if not endpoint_url:
            # An external producer (API-8) posts its own pre-labels; there is
            # nothing to call.
            raise ModelUnavailable(
                "This model has no endpoint: it is an external producer that posts "
                "its own pre-labels."
            )
        if ModelIdentity(identity_type) in ENTRA_IDENTITIES:
            auth = await entra_auth(identity_type, secret_ref, identity_config)
            return cls(endpoint_url, {}, auth)
        return cls(endpoint_url, await auth_headers(identity_type, secret_ref))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._auth is not None:
            await self._auth.aclose()

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"{method} {path}: {exc}") from exc
        if response.status_code >= 500:
            raise ModelUnavailable(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ModelRejected(
                f"{method} {path}: HTTP {response.status_code} {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ModelRejected(f"{method} {path}: response is not JSON") from exc

    async def info(self) -> dict[str, Any]:
        """`GET /info`: identity, classes and media types the model serves."""
        data = await self._request("GET", "/info", timeout=_INFO_TIMEOUT_S)
        if not isinstance(data, dict):
            raise ModelRejected("GET /info: expected a JSON object")
        return data

    async def predict(
        self,
        items: list[PredictItem],
        schema: LabelSchemaDefinition,
        *,
        confidence_threshold: float = 0.0,
    ) -> list[Prediction]:
        """`POST /predict` for one batch (at most :data:`MAX_PREDICT_ITEMS`)."""
        if len(items) > MAX_PREDICT_ITEMS:
            raise ValueError(f"at most {MAX_PREDICT_ITEMS} items per predict call")
        if not items:
            return []
        body = {
            # `media_type` only when it is not an image: models written before
            # text pre-labelling may refuse unknown keys (CONTRACTS.md, text items).
            "items": [
                {
                    "id": i.id,
                    "url": i.url,
                    "width": i.width,
                    "height": i.height,
                    **({"media_type": i.media_type} if i.media_type != "image" else {}),
                }
                for i in items
            ],
            "schema": schema.model_dump(mode="json"),
            "confidence_threshold": confidence_threshold,
        }
        data = await self._request("POST", "/predict", timeout=_PREDICT_TIMEOUT_S, json=body)
        raw = data.get("predictions") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ModelRejected("POST /predict: expected {'predictions': [...]}")
        predictions: list[Prediction] = []
        for entry in raw:
            try:
                predictions.append(
                    Prediction(
                        item_id=str(entry["item_id"]),
                        result=AnnotationResult.model_validate(entry["result"]),
                        confidence=float(entry.get("confidence", 0.0)),
                        error=entry.get("error"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelRejected(f"POST /predict: malformed prediction entry: {exc}") from exc
        return predictions

    async def interactive(
        self,
        item: PredictItem,
        *,
        point: tuple[float, float] | None = None,
        box: tuple[float, float, float, float] | None = None,
    ) -> InteractivePolygon:
        """`POST /interactive` (ML-7): one click or box → one polygon.

        A person is waiting on this, so the timeout is short and the answer is
        checked strictly — a polygon with fewer than three vertices or points
        that are not numbers is a rejection, not something to hand the canvas.
        Points are clamped to the image so a sloppy model cannot put a vertex
        outside it.
        """
        body: dict[str, Any] = {
            "item_id": item.id,
            "url": item.url,
            "width": item.width,
            "height": item.height,
        }
        if point is not None:
            body["point"] = {"x": point[0], "y": point[1]}
        if box is not None:
            body["box"] = {"bbox": list(box)}
        data = await self._request(
            "POST", "/interactive", timeout=_INTERACTIVE_TIMEOUT_S, json=body
        )
        raw = data.get("points") if isinstance(data, dict) else None
        if not isinstance(raw, list) or len(raw) < 3:
            raise ModelRejected("POST /interactive: expected {'points': [[x, y], …≥3]}")
        try:
            points = [
                (
                    min(max(float(p[0]), 0.0), float(item.width)),
                    min(max(float(p[1]), 0.0), float(item.height)),
                )
                for p in raw
            ]
            confidence = float(data.get("confidence", 0.0))
        except (IndexError, TypeError, ValueError) as exc:
            raise ModelRejected(f"POST /interactive: malformed polygon: {exc}") from exc
        return InteractivePolygon(points=points, confidence=confidence)

    async def ocr(self, item: PredictItem, page: int) -> OcrPage:
        """`POST /ocr`: the words of one page of a pdf item, in its points.

        The answer is checked like `/interactive`'s: a page other than the one
        asked for, or a size that is not positive, is a rejection. Words that
        are not text with four numbers are dropped, and boxes are clamped to
        the page, so a sloppy model cannot put text outside it.
        """
        body = {"item_id": item.id, "url": item.url, "media_type": "pdf", "pages": [page]}
        data = await self._request("POST", "/ocr", timeout=_OCR_TIMEOUT_S, json=body)
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
            raise ModelRejected("POST /ocr: expected {'pages': [one page]}")
        raw = pages[0]
        try:
            number = int(raw["page"])
            width, height = float(raw["width"]), float(raw["height"])
            entries = raw["words"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelRejected(f"POST /ocr: malformed page: {exc}") from exc
        if number != page or width <= 0 or height <= 0 or not isinstance(entries, list):
            raise ModelRejected(f"POST /ocr: expected page {page} with a positive size")
        words: list[tuple[str, tuple[float, float, float, float]]] = []
        for entry in entries[:MAX_OCR_WORDS]:
            try:
                text = " ".join(str(entry["text"]).split())
                x0, y0, x1, y1 = (float(v) for v in entry["bbox"])
            except (KeyError, TypeError, ValueError):
                continue
            box = (
                min(max(min(x0, x1), 0.0), width),
                min(max(min(y0, y1), 0.0), height),
                min(max(max(x0, x1), 0.0), width),
                min(max(max(y0, y1), 0.0), height),
            )
            if text and box[2] > box[0] and box[3] > box[1]:
                words.append((text, box))
        engine = data.get("engine")
        return OcrPage(
            page=number,
            width=width,
            height=height,
            engine=engine if isinstance(engine, str) else "unknown",
            words=words,
        )


__all__ = [
    "ENTRA_IDENTITIES",
    "MAX_PREDICT_ITEMS",
    "EntraAuth",
    "InteractivePolygon",
    "ModelClient",
    "ModelError",
    "ModelIdentity",
    "ModelRejected",
    "ModelUnavailable",
    "OcrPage",
    "PredictItem",
    "Prediction",
    "auth_headers",
    "entra_auth",
    "identity_problem",
    "map_result",
    "model_facing_schema",
    "normalise_mapping",
    "validate_mapping",
]
