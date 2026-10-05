# Reference model service

A worked implementation of the annotation platform's model interface (§8 of the
requirements). Two jobs:

1. **Make the demo real.** `docker compose up` gives you working
   model-assisted pre-labelling with no cloud account, no GPU and no weights to
   download.
2. **Be the template you copy (BYOM-4).** Bringing your own model means
   implementing one Protocol and setting one environment variable. The platform
   never changes.

## The contract

| Endpoint | Purpose |
| -------- | ------- |
| `GET /health` | liveness |
| `GET /ready` | readiness; names the loaded backend |
| `GET /info` | model identity, version, classes, media types, GPU |
| `POST /predict` | batch pre-labelling (ML-2) |
| `POST /interactive` | point/box prompt → polygon, sub-second (ML-7) |
| `POST /embed` | vectors for search and clustering (ML-12) |

### `POST /predict`

```json
{
  "items": [{"id": "abc", "url": "https://…signed…", "width": 640, "height": 480}],
  "schema": { "version": 1, "classes": [ … ] },
  "confidence_threshold": 0.25
}
```

```json
{
  "predictions": [{
    "item_id": "abc",
    "confidence": 0.81,
    "error": null,
    "result": {
      "schema_version": 1,
      "media_type": "image",
      "classification": {},
      "shapes": [{
        "id": "3f1c…", "type": "bbox", "class": "car",
        "attributes": {}, "confidence": 0.81,
        "bbox": [120.0, 84.5, 310.0, 240.0]
      }]
    }
  }]
}
```

Three rules the platform relies on:

- **Coordinates are original image pixels**, `[x_min, y_min, x_max, y_max]` —
  never normalised, never letterboxed-input pixels.
- **Every model shape carries a confidence** in 0–1, so low-confidence output
  can be hidden or highlighted (ML-3).
- **Only classes present in the posted schema.** A model that invents a class
  corrupts the project's label set, so unmapped classes are dropped.

A bad item (unreachable URL, unreadable image) returns empty shapes and an
`error` for *that item*. It never fails the batch — one corrupt file must not
block pre-labelling a million-object container.

### `POST /interactive`

```json
{"item_id": "abc", "url": "https://…", "width": 640, "height": 480,
 "point": {"x": 90.0, "y": 75.0}}
```
→ `{"type": "polygon", "points": [[x, y], …], "confidence": 0.7}`

This sits in the annotator's inner loop — someone clicks and waits — so it must
answer in well under a second.

### `POST /ocr`

```json
{"item_id": "abc", "url": "https://…", "media_type": "pdf", "pages": [2]}
```
→ `{"engine": "tesseract", "pages": [{"page": 2, "width": 595.3, "height": 841.9,
"words": [{"text": "42,00", "bbox": [402.5, 611.0, 440.1, 622.8], "confidence": 0.93}]}]}`

The words of scanned pages, for PDFs without a text layer. Boxes are in the
page's points (top-left origin, `/Rotate` applied: the space pdf shapes use),
or in pixels for `media_type: "image"`. `pages` defaults to the first
`OCR_MAX_PAGES`. 501 when no engine is configured, 502 when the engine fails
(a timeout, a refusal, an unparsable answer). Register the service as a model
with task `ocr` and the annotator offers "Read text (OCR)" on scanned pages.

## OCR engines

| `OCR_ENGINE` | What reads the page |
| ------------ | ------------------- |
| `auto` (default) | `tesseract` when the binary is installed (it is in the image), else none |
| `tesseract` | Tesseract, locally. Nothing leaves the service. `OCR_LANGUAGES=fin+eng` for more languages (build with `--build-arg TESSERACT_LANGS="fin swe"`) |
| `anthropic` | Claude with vision, through the official `anthropic` SDK (the `llm` extra, in the image). Needs `ANTHROPIC_API_KEY`; `ANTHROPIC_BASE_URL` for a gateway |
| `openai` | Any OpenAI-compatible `/chat/completions` endpoint with vision: OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio… `OCR_LLM_BASE_URL` + `OCR_LLM_MODEL` |
| `none` | No OCR |

The LLM engines render each page (200 dpi, at most `OCR_LLM_MAX_SIDE` px on the
long side) and ask for every word with its box on a 0–1000 grid of the image,
as JSON (structured output where the endpoint supports it). The answer is
parsed defensively: malformed entries are dropped and boxes clamped to the
page. They are usually better than Tesseract on handwriting, stamps and poor
scans, and slower and not free: one request per page.

**The LLM engines send page images to the endpoint you configure.** Use them
only where your data may go there — a self-hosted vLLM or Ollama keeps pages
in your network.

Claude defaults: `claude-opus-5`, effort `medium`, and Anthropic's server-side
refusal fallback (`fallbacks: "default"`), so a policy decline is retried on
the recommended fallback model rather than failing the page. For a model or
platform without these, set `OCR_LLM_EFFORT=` (empty) and `OCR_LLM_FALLBACKS=off`.

Pre-labelling uses the same engine: pages of a pdf item without a text layer
are read before the document-field rules run, so amounts, dates and e-mails
are found on scans too.

## Backends

| `MODEL_BACKEND` | What it is |
| --------------- | ---------- |
| `heuristic` (default) | Edge-energy detector. No weights, no network, works offline and in CI. |
| `onnx` | Loads a detector from `MODEL_PATH` via onnxruntime. |

**The heuristic backend is not a useful model.** It finds high-contrast regions
and calls them objects. It exists so the pre-labelling loop, the correction
metrics and the review flow can be demonstrated end to end without shipping
weights. Do not benchmark anything against it.

It *is* deterministic — the same image always gives the same boxes — which
matters, because correction-rate metrics (ML-5) are meaningless against a model
that answers differently each run.

Besides images it serves `text` (capitalisation-rule NER, `backends/text_ner.py`)
and `pdf` items (`backends/doc_fields.py`): PDFium reads the text layer, and
words that look like an amount (a neighbouring currency word is merged in),
a date or an e-mail address become `bbox` shapes with `page` and `text`, for
schema classes named `amount`, `date` and `email`. Coordinates are the page's
PDF points, top-left origin, `/Rotate` applied — what the annotator uses.

### ONNX

```sh
MODEL_BACKEND=onnx MODEL_PATH=/models/detector.onnx
```

Put class names in a sibling `detector.names` (one per line) and they are
reported by `/info` for class mapping (BYOM-2).

ONNX rather than pickle is deliberate: BYOM-8 forbids loading customer-supplied
models through `pickle`, which executes arbitrary code on deserialisation.

The fiddly part is the **letterbox coordinate mapping**. A detector takes a
fixed square input, so the image is scaled and padded; every predicted box is in
padded-input pixels and must be mapped back. Getting the padding offset wrong
produces boxes that look plausible but drift consistently toward one corner —
so `LetterboxTransform` is a separate, unit-tested class.

## Wrapping your own model

```python
class MyBackend:
    name = "my-detector"
    version = "2.1.0"
    gpu = True
    media_types = [MediaType.IMAGE]

    def classes(self) -> list[str]: ...
    async def predict(self, items, schema, confidence_threshold): ...
    async def interactive(self, request): ...
    async def embed(self, items): ...
```

Register it in `app/backends/__init__.py`, set `MODEL_BACKEND=my-detector`, and
the platform is unchanged. Or ignore this service entirely and expose the same
six endpoints from your own stack — that is the whole point of the contract.

## Environment

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `MODEL_BACKEND` | `heuristic` | which backend to load |
| `MODEL_PATH` | — | `.onnx` file, required by the onnx backend |
| `PORT` | `9000` | listen port |
| `MODEL_API_KEY` | — | when set, every route except `/health` and `/ready` needs `Authorization: Bearer <key>` (see below) |
| `OCR_ENGINE` | `auto` | `auto`, `none`, `tesseract`, `anthropic`, `openai` (above) |
| `OCR_MAX_PAGES` | `20` | pages read per `/ocr` request or pre-labelled item |
| `OCR_LANGUAGES` | `eng` | Tesseract languages, `+`-joined |
| `OCR_MIN_CONFIDENCE` | `0.3` | Tesseract words below this are dropped |
| `OCR_TIMEOUT` | `60` | seconds per page for Tesseract |
| `TESSERACT_BIN` | `tesseract` | the binary |
| `OCR_LLM_MODEL` | `claude-opus-5` (anthropic) | model name; required for `openai` |
| `OCR_LLM_BASE_URL` | — | `openai`: base URL, `/chat/completions` is appended |
| `OCR_LLM_API_KEY` | — | `openai`: the key, sent as `Authorization: Bearer …` |
| `OCR_LLM_API_KEY_HEADER` | `Authorization` | `openai`: `api-key` for Azure OpenAI |
| `OCR_LLM_RESPONSE_FORMAT` | `json_schema` | `openai`: `json_schema`, `json_object` or `none`, as the server supports |
| `OCR_LLM_EFFORT` | `medium` | `anthropic`: effort; empty omits it |
| `OCR_LLM_FALLBACKS` | `default` | `anthropic`: `off` disables the server-side refusal fallback |
| `OCR_LLM_MAX_SIDE` | `2000` | long side of the page image sent to an LLM, px |
| `OCR_LLM_TIMEOUT` | `120` | seconds per page for an LLM |
| `ANTHROPIC_API_KEY` | — | `anthropic`: read by the SDK |

### Securing the service

The service is open by default, which is fine on a private compose network. For
anything reachable from elsewhere set `MODEL_API_KEY` to a long random value;
requests without a matching `Authorization: Bearer <key>` get `401`. `/health`
and `/ready` stay open for probes. To let the platform call it, register the
model with `identity_type: "bearer"` and a `secret_ref` that resolves to the same
key (for example `env:MODEL_API_KEY_SECRET` or a Key Vault reference, like any
other model secret); the platform then sends `Authorization: Bearer <key>` on
every call.

The service fetches the item URLs it is sent. It accepts only `http(s)` and
`data:` URLs, follows no redirects, and refuses cloud metadata endpoints
(`169.254.0.0/16`, `fe80::/10`, `fd00:ec2::254`, `100.100.100.200`,
`metadata.google.internal`), but private addresses stay allowed because media
normally comes from the platform inside the cluster. Keep it on a network
that cannot reach other hosts you do not want it to.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,llm]"
# Tesseract tests run when the binary is installed (apt install tesseract-ocr).
.venv/bin/pytest
.venv/bin/uvicorn app.main:app --port 9000
```
