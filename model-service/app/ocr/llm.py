"""OCR through an external vision LLM.

Two engines share one prompt and one answer format — every word with its box
on a 0-1000 grid of the image, which vision models localise more reliably
than raw pixels:

- `AnthropicEngine` calls Claude with the official `anthropic` SDK
  (`pip install '.[llm]'`), structured output guaranteeing the JSON shape.
  The key comes from `ANTHROPIC_API_KEY` (or any credential the SDK
  resolves); `ANTHROPIC_BASE_URL` points it at a gateway.
- `OpenAICompatibleEngine` posts to `{OCR_LLM_BASE_URL}/chat/completions`,
  the dialect OpenAI, Azure OpenAI, vLLM, Ollama and LM Studio all speak.

The answer is untrusted model output: it is parsed defensively, coordinates
are clamped and malformed entries dropped, and it only ever becomes word
boxes — never instructions.
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

import httpx
from PIL import Image

from app.ocr import OcrError, OcrWord

#: The grid the model answers on; scaled to the image's pixels on the way in.
GRID = 1000
#: Words kept per page; a real page has far fewer, nonsense does not grow unbounded.
MAX_WORDS = 5000
#: Images above this many bytes as PNG are sent as JPEG instead.
MAX_PNG_BYTES = 3_500_000

PROMPT = (
    "This image is one page of a scanned document. Transcribe every word on it, "
    "in reading order, exactly as written: do not correct, translate or summarise. "
    "One entry per whitespace-separated word, with its bounding box as integers on "
    f"a 0-{GRID} grid of the image: x0, y0 the top-left corner and x1, y1 the "
    f"bottom-right corner, where x is 0 at the left edge and {GRID} at the right edge "
    f"and y is 0 at the top edge and {GRID} at the bottom edge. "
    "If the page has no text, answer with an empty list. Answer with JSON: "
    '{"words": [{"text": "...", "x0": 0, "y0": 0, "x1": 0, "y1": 0}, ...]}'
)

_COORDS = ("x0", "y0", "x1", "y1")

#: The answer's JSON Schema (structured output on both engines).
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    **{name: {"type": "integer"} for name in _COORDS},
                },
                "required": ["text", *_COORDS],
                "additionalProperties": False,
            },
        }
    },
    "required": ["words"],
    "additionalProperties": False,
}


def encode_image(image: Image.Image) -> tuple[str, str]:
    """`(media_type, base64)` for the page: PNG keeps glyph edges; JPEG if too big."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    if buffer.tell() > MAX_PNG_BYTES:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        return "image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii")
    return "image/png", base64.b64encode(buffer.getvalue()).decode("ascii")


def extract_json(text: str) -> Any:
    """The JSON object in a model's text, tolerating a Markdown fence or a preamble."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise OcrError("the model's answer holds no JSON object") from None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OcrError(f"the model's answer is not valid JSON: {exc}") from exc


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(max(float(value), 0.0), float(GRID))


def parse_words(payload: Any, width: int, height: int) -> list[OcrWord]:
    """Words on the 0-1000 grid → words in the image's pixels.

    Accepts `{x0, y0, x1, y1}` as asked, or a `box` / `bbox` list, since an
    OpenAI-compatible model without structured output may improvise.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        raise OcrError("the model's answer has no 'words' list")
    sx, sy = width / GRID, height / GRID
    words: list[OcrWord] = []
    for entry in payload["words"][:MAX_WORDS]:
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            continue
        box = entry.get("box", entry.get("bbox"))
        raw = box if isinstance(box, list) and len(box) == 4 else [entry.get(k) for k in _COORDS]
        coords = [_number(v) for v in raw]
        if any(c is None for c in coords):
            continue
        x0, y0, x1, y1 = (c or 0.0 for c in coords)
        text = entry["text"].strip()
        if text and x1 != x0 and y1 != y0:
            words.append(OcrWord(text, (x0 * sx, y0 * sy, x1 * sx, y1 * sy)))
    return words


class AnthropicEngine:
    """Claude reads the page (vision + structured output), via the `anthropic` SDK."""

    name = "anthropic"
    dpi = 200

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        effort: str | None = "medium",
        fallbacks: bool = True,
        max_side: int = 2000,
        timeout: float = 120.0,
        client: Any = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise ValueError(
                    "OCR_ENGINE=anthropic needs the 'anthropic' package: pip install '.[llm]'"
                ) from exc
            client = anthropic.AsyncAnthropic(timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.max_side = max_side
        self._client = client

    @classmethod
    def from_env(cls) -> AnthropicEngine:
        return cls(
            model=os.environ.get("OCR_LLM_MODEL") or "claude-opus-5",
            effort=os.environ.get("OCR_LLM_EFFORT", "medium") or None,
            fallbacks=os.environ.get("OCR_LLM_FALLBACKS", "default") != "off",
            max_side=int(os.environ.get("OCR_LLM_MAX_SIDE", "2000")),
            timeout=float(os.environ.get("OCR_LLM_TIMEOUT", "120")),
        )

    async def read(self, image: Image.Image) -> list[OcrWord]:
        import anthropic

        media_type, data = encode_image(image)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": SCHEMA}}
        if self.effort:
            output_config["effort"] = self.effort
        extra: dict[str, Any] = {}
        if self.fallbacks:
            # On a policy decline the API re-runs the request on Anthropic's
            # recommended fallback model instead of returning the refusal.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            # Streamed: a dense page is a long answer, past non-streaming timeouts.
            async with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                output_config=output_config,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
                **extra,
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise OcrError(f"Claude answered {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise OcrError(f"cannot reach the Claude API: {exc}") from exc
        if message.stop_reason == "refusal":
            raise OcrError("the model declined to read this page")
        if message.stop_reason == "max_tokens":
            raise OcrError("the page holds more text than the model could return")
        text = "".join(block.text for block in message.content if block.type == "text")
        return parse_words(extract_json(text), image.width, image.height)


class OpenAICompatibleEngine:
    """Any `/chat/completions` endpoint with vision: OpenAI, Azure, vLLM, Ollama, …"""

    name = "openai"
    dpi = 200

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_header: str = "Authorization",
        response_format: str = "json_schema",
        max_side: int = 2000,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if response_format not in ("json_schema", "json_object", "none"):
            raise ValueError("OCR_LLM_RESPONSE_FORMAT must be json_schema, json_object or none")
        headers: dict[str, str] = {}
        if api_key:
            headers[api_key_header] = (
                f"Bearer {api_key}" if api_key_header.lower() == "authorization" else api_key
            )
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.response_format = response_format
        self.max_side = max_side
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    @classmethod
    def from_env(cls) -> OpenAICompatibleEngine:
        base_url = os.environ.get("OCR_LLM_BASE_URL", "")
        model = os.environ.get("OCR_LLM_MODEL", "")
        if not base_url or not model:
            raise ValueError("OCR_ENGINE=openai needs OCR_LLM_BASE_URL and OCR_LLM_MODEL")
        return cls(
            base_url=base_url,
            model=model,
            api_key=os.environ.get("OCR_LLM_API_KEY") or None,
            api_key_header=os.environ.get("OCR_LLM_API_KEY_HEADER", "Authorization"),
            response_format=os.environ.get("OCR_LLM_RESPONSE_FORMAT", "json_schema"),
            max_side=int(os.environ.get("OCR_LLM_MAX_SIDE", "2000")),
            timeout=float(os.environ.get("OCR_LLM_TIMEOUT", "120")),
        )

    async def read(self, image: Image.Image) -> list[OcrWord]:
        media_type, data = encode_image(image)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"},
                        },
                    ],
                }
            ],
        }
        if self.response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "page_words", "schema": SCHEMA, "strict": True},
            }
        elif self.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.post(self.url, json=body)
        except httpx.HTTPError as exc:
            raise OcrError(f"cannot reach {self.url}: {exc}") from exc
        if response.status_code >= 400:
            raise OcrError(f"{self.url} answered {response.status_code}: {response.text[:200]}")
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OcrError("the endpoint's answer is not a chat completion") from exc
        if choice.get("finish_reason") == "length":
            raise OcrError("the page holds more text than the model could return")
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise OcrError("the model returned no text")
        return parse_words(extract_json(content), image.width, image.height)
