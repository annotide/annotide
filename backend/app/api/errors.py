"""RFC 9457 problem details for every error the API returns (API-2).

FastAPI's default error body is ``{"detail": ...}``, which differs in shape
between a validation error and an HTTPException. Clients then need two parsers.
These handlers normalise everything onto one envelope::

    {"type": "...", "title": "...", "status": 409, "detail": "..."}

``type`` is a stable URN the frontend can switch on; ``detail`` is prose for a
human and may change freely.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.pagination import InvalidCursorError
from app.services.scim import SCIM_CONTENT_TYPE, ScimError
from app.services.workflow import WorkflowError

PROBLEM_CONTENT_TYPE = "application/problem+json"

_URN_PREFIX = "urn:annotation:error:"

# Starlette renamed 422 from UNPROCESSABLE_ENTITY to UNPROCESSABLE_CONTENT and
# deprecated the old name. The number is stable across every version, so use it.
HTTP_422_UNPROCESSABLE = 422


class ApiError(Exception):
    """Base for errors the API raises deliberately.

    Subclasses set ``status_code``, ``error_type`` and ``title``; the message
    passed to ``__init__`` becomes ``detail``.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    error_type: str = "bad-request"
    title: str = "Bad request"

    def __init__(
        self,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra or {}
        self.headers = headers or {}


class NotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not-found"
    title = "Resource not found"


class ForbiddenError(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "forbidden"
    title = "Not allowed"


class SeatLimitError(ForbiddenError):
    """Sign-in refused: the licence has no free seat (LIC-23, LIC-24)."""

    error_type = "seat-limit"
    title = "No free licence seat"


class LicenceRestrictedError(ForbiddenError):
    """Restricted mode: the licence expired and its grace period is over (LIC-5)."""

    error_type = "license-restricted"
    title = "Licence expired"


class LicenceFeatureError(ForbiddenError):
    """A Business feature the licence in force does not unlock (LIC-33)."""

    error_type = "license-feature"
    title = "Business feature"


class InvalidLicenseKeyError(ApiError):
    """A pasted licence key is malformed, forged or already expired (LIC-26)."""

    status_code = HTTP_422_UNPROCESSABLE
    error_type = "invalid-license-key"
    title = "Invalid licence key"


class UnauthorizedError(ApiError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "unauthorized"
    title = "Authentication required"


class MfaSetupRequiredError(ForbiddenError):
    """`APP_MFA_REQUIRED_FOR_ADMINS`: this administrator must turn MFA on first."""

    error_type = "mfa-setup-required"
    title = "Set up an authenticator app first"


class MfaRequiredError(UnauthorizedError):
    """The password was right; this account also needs a second factor (AUTH-2)."""

    error_type = "mfa-required"
    title = "Authentication code required"


class MfaInvalidError(UnauthorizedError):
    """Sign-in refused: the authentication code was wrong or already used (AUTH-2)."""

    error_type = "mfa-invalid"
    title = "Invalid authentication code"


class MfaCodeRejectedError(ApiError):
    """A code given to confirm an MFA change was wrong (AUTH-2)."""

    status_code = HTTP_422_UNPROCESSABLE
    error_type = "mfa-invalid"
    title = "Invalid authentication code"


class ConflictError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "conflict"
    title = "Conflicting state"


class TaskLockedError(ConflictError):
    """Someone else holds the lock on this task (WF-3)."""

    error_type = "task-locked"
    title = "Task is locked by another user"


class TrialUnavailableError(ConflictError):
    """No trial for this installation (LIC-34)."""

    error_type = "trial-unavailable"
    title = "Trial not available"


class ServiceUnavailableError(ApiError):
    """A dependency the request needs (the job queue) is not reachable."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "service-unavailable"
    title = "Service temporarily unavailable"


class ModelUnavailableError(ApiError):
    """A customer model endpoint could not be reached or gave a bad answer (ML-7)."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "model-unavailable"
    title = "Model endpoint unavailable"


class TrialServiceError(ApiError):
    """The licence server could not issue a trial key (LIC-34)."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "trial-service"
    title = "Licence server unavailable"


class PayloadTooLargeError(ApiError):
    """An upload is over the size the endpoint accepts (EXP-6 imports)."""

    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_type = "payload-too-large"
    title = "Payload too large"


class RateLimitedError(ApiError):
    """Too many requests in the current window (API-5); carries `Retry-After`."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate-limited"
    title = "Too many requests"

    def __init__(self, detail: str, *, retry_after: int, limit: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)}
        if limit is not None:
            headers |= {
                "RateLimit-Limit": str(limit),
                "RateLimit-Remaining": "0",
                "RateLimit-Reset": str(retry_after),
            }
        super().__init__(detail, extra={"retry_after": retry_after}, headers=headers)


class ValidationFailedError(ApiError):
    """Annotation failed the pre-submit rules (QA-6)."""

    status_code = HTTP_422_UNPROCESSABLE
    error_type = "validation-failed"
    title = "Annotation failed validation"


def problem_response(
    *,
    status_code: int,
    error_type: str,
    title: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build one problem-details response."""
    body: dict[str, Any] = {
        "type": f"{_URN_PREFIX}{error_type}",
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler. Called once from ``app.main``."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        response = problem_response(
            status_code=exc.status_code,
            error_type=exc.error_type,
            title=exc.title,
            detail=exc.detail,
            extra=exc.extra,
        )
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(ScimError)
    async def _scim_error(_: Request, exc: ScimError) -> JSONResponse:
        # SCIM clients expect RFC 7644 error bodies, not problem details (AUTH-3).
        return JSONResponse(
            status_code=exc.status,
            content=exc.body(),
            media_type=SCIM_CONTENT_TYPE,
            headers=exc.headers,
        )

    @app.exception_handler(WorkflowError)
    async def _workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        # An illegal transition is a state conflict, not a malformed request.
        return problem_response(
            status_code=status.HTTP_409_CONFLICT,
            error_type="illegal-transition",
            title="Illegal workflow transition",
            detail=str(exc),
            extra={"source_status": exc.source.value, "trigger": exc.trigger.value},
        )

    @app.exception_handler(InvalidCursorError)
    async def _invalid_cursor(_: Request, exc: InvalidCursorError) -> JSONResponse:
        return problem_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid-cursor",
            title="Invalid pagination cursor",
            detail=str(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            status_code=HTTP_422_UNPROCESSABLE,
            error_type="invalid-request",
            title="Request body failed validation",
            detail="One or more fields are invalid.",
            # Pydantic's per-field errors, kept under their own key so the
            # top-level shape stays constant. A custom validator's `ValueError`
            # rides along in `ctx` and is not JSON on its own.
            extra={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(
            status_code=exc.status_code,
            error_type="http-error",
            title=str(exc.detail),
            detail=str(exc.detail),
        )
