# syntax=docker/dockerfile:1
#
# backend/Dockerfile — multi-stage build for the FastAPI API service AND the
# arq worker: both are entrypoints of the same package, so compose runs this
# one image twice with different commands (see docker-compose.yml `worker`).
#
# Stage 1 (builder): install the project (and its locked deps) into a venv.
# Stage 2 (runtime): copy only the venv + app source into a slim, non-root
# image. Keep this stage lean — heavier tooling (compilers, headers) stays
# in the builder stage only.

ARG PYTHON_VERSION=3.12-slim

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

# Build-time OS packages: a C toolchain is needed to build wheels for a few
# transitive deps (e.g. cryptography, argon2-cffi) that may not ship
# manylinux wheels for every platform. Kept out of the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Create the venv that gets copied verbatim into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copy only dependency metadata first so this layer caches across source
# changes; the project itself is installed via pyproject.toml, so we need
# the app package present for the editable/no-editable build to resolve.
COPY pyproject.toml ./
COPY app ./app

# Install the project (non-editable) into the venv. No dev extras here —
# those are only needed in CI. EXTRAS adds optional ones, e.g.
# `--build-arg EXTRAS=otel` for OpenTelemetry (OPS-3); empty by default so
# the standard image stays lean.
ARG EXTRAS=""
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".${EXTRAS:+[$EXTRAS]}"

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

# Runtime OS packages, kept minimal:
#   - curl: used by the HEALTHCHECK below to probe the API.
#   - libpq5: runtime shared lib for asyncpg's C extension / psycopg-style
#     libpq usage on some platforms.
# Tiling (IMG-1, `tile_image` job) uses libvips through the `pyvips[binary]`
# wheel, which bundles libvips — no apt package is needed. That build has no
# OpenSlide, so whole-slide formats (.svs, .ndpi, .mrxs) are not readable;
# supporting them means installing libvips + libopenslide here and dropping
# the `binary` extra. ffmpeg (video decoding) is not needed yet: the browser
# plays video, the worker never decodes it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /usr/sbin/nologin --no-create-home app

# Bring in the venv built above — no build tools, no source archives.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Application source, including app/worker/ (the arq entrypoint).
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

# The worker overrides this with: arq app.worker.main.WorkerSettings
# Keep-alive outlasts a proxy's idle timeout (ingress-nginx and most load
# balancers: 60 s); at uvicorn's 5 s the proxy reuses a connection the server
# is closing and the request fails with a reset.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
