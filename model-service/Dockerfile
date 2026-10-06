# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
# `llm`: the Anthropic SDK for OCR_ENGINE=anthropic.
RUN pip install ".[llm]"

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    MODEL_BACKEND=heuristic

# Tesseract language packs beyond English, e.g. "fin swe deu" (OCR_LANGUAGES
# then picks them, as "fin+eng").
ARG TESSERACT_LANGS="fin"

# curl is only for the healthcheck; libgomp is needed by numpy's threaded ops;
# tesseract-ocr is the local OCR engine for scanned PDFs (OCR_ENGINE=tesseract).
RUN apt-get update && apt-get install --no-install-recommends -y curl libgomp1 tesseract-ocr \
        $(for lang in $TESSERACT_LANGS; do echo "tesseract-ocr-$lang"; done) \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 model

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
RUN chown -R model:model /app

USER model
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:9000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
