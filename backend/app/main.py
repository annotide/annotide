"""FastAPI application factory.

Import path used by the container and by uvicorn: ``app.main:app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1 import api_router
from app.core import observability
from app.core.config import Settings, get_settings
from app.core.entra import aclose_token_sources
from app.core.logging import configure_logging, get_logger
from app.core.proxy import ProxyHeadersMiddleware
from app.core.rate_limit_headers import RateLimitHeadersMiddleware
from app.db.session import get_engine
from app.services.queue import aclose_job_queue
from app.services.rate_limit import aclose_rate_limiter
from app.services.secrets import aclose_backends
from app.services.secrets import configure as configure_secrets
from app.services.storage import aclose_storage

API_V1_PREFIX = "/api/v1"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on the way up and dispose of the engine on the way down."""
    settings = get_settings()
    configure_logging()
    configure_secrets(
        file_root=settings.secret_file_root,
        azure_default_vault=settings.azure_key_vault_name,
        azure_client_id=settings.managed_identity_client_id,
        aws_default_region=settings.aws_region,
        cache_ttl=settings.secret_cache_ttl,
    )
    if observability.is_enabled():
        observability.instrument_engine(get_engine().sync_engine)
    log.info("api.starting", env=settings.env, version=app.version, otel=observability.is_enabled())
    yield
    await aclose_job_queue()
    await aclose_storage()
    await aclose_rate_limiter()
    await aclose_backends()
    await get_engine().dispose()
    await aclose_token_sources()
    observability.shutdown()
    log.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests call this directly with their own settings."""
    settings = settings or get_settings()

    app = FastAPI(
        title="Annotide API",
        version="0.1.0",
        summary="Cloud-agnostic annotation platform",
        description=(
            "Data stays in the customer's own storage; this API serves metadata, "
            "annotations and short-lived signed URLs."
        ),
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # The frontend reads the next page's cursor from the body, but exposing
        # these lets it surface rate-limit state without a preflight surprise.
        expose_headers=[
            "X-Request-Id",
            "Retry-After",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
        ],
    )
    # The window `deps.enforce_rate_limit` recorded, as `RateLimit-*` headers.
    app.add_middleware(RateLimitHeadersMiddleware)

    if settings.trusted_proxies:
        # Added last so it runs first: request.client and request.url are
        # already rewritten by the time CORS, dependencies or handlers look.
        app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=settings.trusted_proxies)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_V1_PREFIX)

    if settings.otel_enabled:
        observability.configure("annotation-api")
        observability.instrument_app(app)

    return app


app = create_app()
