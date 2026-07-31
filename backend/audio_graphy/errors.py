"""Unified error handling for AudioGraphy M3.

Defines the ``APIError`` exception hierarchy and FastAPI exception handlers
that produce a consistent JSON error envelope:

    {"error": {"code": "...", "message": "...", "detail": {...}}}

See: docs/m3-architecture.md §7.1, docs/m3-prd.md QUAL-01.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ============================================================
# Error code constants (must match PRD §4 + architecture §7.1)
# ============================================================

# 400
CODE_FILE_NOT_FOUND = "FILE_NOT_FOUND"

# 401
CODE_INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
CODE_TOKEN_EXPIRED = "TOKEN_EXPIRED"
CODE_INVALID_TOKEN = "INVALID_TOKEN"
CODE_INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"

# 403
CODE_FORBIDDEN = "FORBIDDEN"

# 404
CODE_RECORDING_NOT_FOUND = "RECORDING_NOT_FOUND"
CODE_ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
CODE_PROMPT_NOT_FOUND = "PROMPT_NOT_FOUND"
CODE_PATH_NOT_FOUND = "PATH_NOT_FOUND"
CODE_TASK_NOT_FOUND = "TASK_NOT_FOUND"

# 409
CODE_DUPLICATE_RECORDING = "DUPLICATE_RECORDING"
CODE_DUPLICATE_PROMPT_VERSION = "DUPLICATE_PROMPT_VERSION"
CODE_RECORDING_NOT_INDEXED = "RECORDING_NOT_INDEXED"

# 422
CODE_VALIDATION_ERROR = "VALIDATION_ERROR"

# 500
CODE_INTERNAL_ERROR = "INTERNAL_ERROR"


# ============================================================
# APIError hierarchy
# ============================================================


class APIError(Exception):
    """Base class for all API errors.

    Attributes:
        code: Machine-readable error code (e.g. ``RECORDING_NOT_FOUND``).
        message: Human-readable error message.
        detail: Optional structured detail dict.
        status_code: HTTP status code.
    """

    code: str = CODE_INTERNAL_ERROR
    status_code: int = 500
    message: str = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.detail: dict[str, Any] = detail or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the standard error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


# --- 400 ---


class BadRequestError(APIError):
    code = CODE_FILE_NOT_FOUND
    status_code = 400


class FileNotFoundError400(APIError):
    code = CODE_FILE_NOT_FOUND
    status_code = 400
    message = "File not found"


# --- 401 ---


class UnauthorizedError(APIError):
    code = CODE_INVALID_TOKEN
    status_code = 401
    message = "Unauthorized"


class InvalidCredentialsError(APIError):
    code = CODE_INVALID_CREDENTIALS
    status_code = 401
    message = "Invalid email or password"


class TokenExpiredError(APIError):
    code = CODE_TOKEN_EXPIRED
    status_code = 401
    message = "Access token has expired"


class InvalidTokenError(APIError):
    code = CODE_INVALID_TOKEN
    status_code = 401
    message = "Invalid token"


class InvalidRefreshTokenError(APIError):
    code = CODE_INVALID_REFRESH_TOKEN
    status_code = 401
    message = "Invalid or expired refresh token"


# --- 403 ---


class ForbiddenError(APIError):
    code = CODE_FORBIDDEN
    status_code = 403
    message = "Forbidden: insufficient role privileges"


# --- 404 ---


class NotFoundError(APIError):
    code = CODE_RECORDING_NOT_FOUND
    status_code = 404
    message = "Resource not found"


class RecordingNotFoundError(APIError):
    code = CODE_RECORDING_NOT_FOUND
    status_code = 404
    message = "Recording not found"


class EntityNotFoundError(APIError):
    code = CODE_ENTITY_NOT_FOUND
    status_code = 404
    message = "Graph entity not found"


class PromptNotFoundError(APIError):
    code = CODE_PROMPT_NOT_FOUND
    status_code = 404
    message = "Prompt not found"


class PathNotFoundError(APIError):
    code = CODE_PATH_NOT_FOUND
    status_code = 404
    message = "No path found between the given entities"


class TaskNotFoundError(APIError):
    code = CODE_TASK_NOT_FOUND
    status_code = 404
    message = "Recompute task not found"


# --- 409 ---


class ConflictError(APIError):
    code = CODE_DUPLICATE_RECORDING
    status_code = 409
    message = "Conflict"


class DuplicateRecordingError(APIError):
    code = CODE_DUPLICATE_RECORDING
    status_code = 409
    message = "Recording already registered for this tenant"


class DuplicatePromptVersionError(APIError):
    code = CODE_DUPLICATE_PROMPT_VERSION
    status_code = 409
    message = "Prompt (name, version) already exists"


class RecordingNotIndexedError(APIError):
    code = CODE_RECORDING_NOT_INDEXED
    status_code = 409
    message = "Recording is not indexed yet, cannot tag"


# --- 422 ---


class ValidationError(APIError):
    code = CODE_VALIDATION_ERROR
    status_code = 422
    message = "Validation error"


# --- 500 ---


class InternalError(APIError):
    code = CODE_INTERNAL_ERROR
    status_code = 500
    message = "Internal server error"


# ============================================================
# FastAPI exception handler registration
# ============================================================


def _envelope(code: str, message: str, detail: dict[str, Any]) -> dict[str, Any]:
    """Build the standard error envelope."""
    return {"error": {"code": code, "message": message, "detail": detail}}


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on a FastAPI app.

    Handles:
        - ``APIError`` subclasses → standard envelope with correct status code.
        - ``RequestValidationError`` (FastAPI 422) → standard envelope with VALIDATION_ERROR.
        - Catch-all ``Exception`` → 500 INTERNAL_ERROR (with request_id in detail).
    """

    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        detail = dict(exc.detail)
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            detail.setdefault("request_id", request_id)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, detail),
            headers={"X-Request-ID": request_id} if request_id else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail: dict[str, Any] = {
            "errors": jsonable_encoder(exc.errors()),
        }
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            detail["request_id"] = request_id
        return JSONResponse(
            status_code=422,
            content=_envelope(CODE_VALIDATION_ERROR, "Validation error", detail),
            headers={"X-Request-ID": request_id} if request_id else None,
        )

    @app.exception_handler(Exception)
    async def _handle_generic_error(request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger(__name__).exception("Unhandled exception: %s", exc)
        detail: dict[str, Any] = {}
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            detail["request_id"] = request_id
        return JSONResponse(
            status_code=500,
            content=_envelope(CODE_INTERNAL_ERROR, "Internal server error", detail),
            headers={"X-Request-ID": request_id} if request_id else None,
        )
