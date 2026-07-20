"""Auth and request-ID middleware.

``AuthMiddleware`` extracts JWT from ``Authorization: Bearer <token>``,
verifies it, and injects ``request.state.user``, ``request.state.tenant_id``,
and ``request.state.agent_filter`` (for agent role).

``RequestIdMiddleware`` injects a UUID4 into ``request.state.request_id``
and adds an ``X-Request-ID`` response header.

See: docs/m3-architecture.md §3.1, §7.2, docs/m3-prd.md AUTH-02/03, QUAL-05.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.errors import InvalidTokenError, TokenExpiredError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@dataclass(frozen=True, slots=True)
class AuthUser:
    """Authenticated user injected into ``request.state.user``.

    Attributes:
        id: User ID.
        name: User display name.
        email: User email.
        role: RBAC role string.
        tenant_id: Tenant ID.
    """

    id: int
    name: str
    email: str
    role: str
    tenant_id: str


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a request_id UUID into request state and response headers.

    - If the incoming request has ``X-Request-ID``, it is reused (distributed tracing).
    - Otherwise, a new UUID4 is generated.
    - The response always includes ``X-Request-ID``.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT authentication + tenant isolation middleware.

    Public paths (no auth required):
        - Paths ending with ``/auth/login``
        - Paths ending with ``/auth/refresh``
        - Paths ending with ``/health/readiness``
        - Paths ending with ``/health`` (liveness)
        - Paths ending with ``/docs`` / ``/openapi.json`` / ``/redoc``

    For all other paths, the middleware:
        1. Extracts the JWT from ``Authorization: Bearer <token>``.
        2. Verifies the access token.
        3. Injects ``request.state.user`` (AuthUser).
        4. Injects ``request.state.tenant_id``.
        5. For agent role, injects ``request.state.agent_filter`` = user name.

    Args:
        jwt_manager: JWTManager instance for token verification.
        public_paths: Set of path suffixes that bypass auth.
    """

    DEFAULT_PUBLIC_SUFFIXES: tuple[str, ...] = (
        "/auth/login",
        "/auth/refresh",
        "/health",
        "/health/readiness",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/",
    )

    def __init__(self, app: object, jwt_manager: JWTManager) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._jwt_manager = jwt_manager

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip auth for public paths
        if any(path.endswith(suffix) or path == suffix for suffix in self.DEFAULT_PUBLIC_SUFFIXES):
            return await call_next(request)

        # Extract and verify token
        token = self._extract_token(request)
        if token is None:
            return self._error_response(
                InvalidTokenError("Missing Authorization header"),
                request,
            )

        try:
            payload = self._jwt_manager.verify_access_token(token)
        except (TokenExpiredError, InvalidTokenError) as exc:
            return self._error_response(exc, request)

        # Inject user info into request state
        # We only have the JWT payload here; the full user object (name/email)
        # is fetched lazily by the dependency. For middleware, we inject
        # a minimal AuthUser using payload fields.
        request.state.user = AuthUser(
            id=payload.sub,
            name="",  # Filled by get_current_user dependency
            email="",
            role=payload.role,
            tenant_id=payload.tid,
        )
        request.state.tenant_id = payload.tid
        if payload.role == "agent":
            # Agent filter is the user's name — fetched in the dependency
            # since we don't have it in the JWT payload.
            request.state.agent_filter = None  # Will be set by get_current_user

        return await call_next(request)

    @staticmethod
    def _extract_token(request: Request) -> str | None:
        """Extract Bearer token from Authorization header."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header:
            return None
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return None

    @staticmethod
    def _error_response(exc: Exception, request: Request) -> Response:
        """Return a JSON error response for auth failures."""
        from audio_graphy.errors import APIError

        api_err = exc if isinstance(exc, APIError) else InvalidTokenError(str(exc))

        import json

        request_id = getattr(request.state, "request_id", None)
        detail: dict[str, object] = {}
        if request_id:
            detail["request_id"] = request_id

        body = json.dumps(
            {
                "error": {
                    "code": api_err.code,
                    "message": api_err.message,
                    "detail": detail,
                }
            },
            ensure_ascii=False,
        )
        return Response(
            content=body,
            media_type="application/json",
            status_code=api_err.status_code,
            headers={"X-Request-ID": request_id} if request_id else None,
        )
