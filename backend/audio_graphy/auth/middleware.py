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
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.errors import InvalidTokenError, TokenExpiredError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_PLAYBACK_AUDIO_PATH = re.compile(
    r"^/api/v1/receptions/[1-9]\d*/(?:audio|recordings/[1-9]\d*/audio)$"
)


class _RequestBodyTooLargeError(Exception):
    """Internal signal raised while consuming a chunked request body."""


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before application-level parsing.

    Both declared ``Content-Length`` and streamed/chunked bodies are counted.
    WebSockets are left untouched.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        declared_length = headers.get(b"content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError:
                parsed_length = -1
            if parsed_length > self.max_body_bytes:
                await self._send_rejection(scope, send)
                return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise _RequestBodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLargeError:
            if response_started:
                raise
            await self._send_rejection(scope, send)

    async def _send_rejection(self, scope: Scope, send: Send) -> None:
        import json

        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore") or str(
            uuid.uuid4()
        )
        detail: dict[str, object] = {
            "max_bytes": self.max_body_bytes,
            "request_id": request_id,
        }
        body = json.dumps(
            {
                "error": {
                    "code": "REQUEST_BODY_TOO_LARGE",
                    "message": "Request body exceeds the configured limit",
                    "detail": detail,
                }
            },
            separators=(",", ":"),
        ).encode()
        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        response_headers.append((b"x-request-id", request_id.encode()))
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


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

    Public paths (no auth required) are exact allow-list entries:
        - ``/api/v1/auth/login`` and ``/api/v1/auth/refresh``
        - ``/health/readiness`` and ``/health``
        - ``/docs``, ``/openapi.json``, ``/redoc`` and the root page

    For all other paths, the middleware:
        1. Extracts the JWT from ``Authorization: Bearer <token>``.
        2. Verifies the access token.
        3. Injects ``request.state.user`` (AuthUser).
        4. Injects ``request.state.tenant_id``.
        5. For agent role, injects ``request.state.agent_filter`` = user name.

    Args:
        jwt_manager: JWTManager instance for token verification.
        Public routes are intentionally exact matches so a protected URL that
        merely ends with a public-looking suffix cannot bypass authentication.
    """

    DEFAULT_PUBLIC_PATHS: frozenset[str] = frozenset(
        {
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/health",
            "/health/readiness",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/",
        }
    )

    def __init__(
        self,
        app: object,
        jwt_manager: JWTManager,
        playback_secret: str | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._jwt_manager = jwt_manager
        self._playback_secret = playback_secret

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip auth for public paths
        if path in self.DEFAULT_PUBLIC_PATHS:
            return await call_next(request)

        # Extract and verify token
        token = self._extract_token(request)
        if token is None:
            grant = request.query_params.get("playback_grant")
            if grant and self._playback_secret and _PLAYBACK_AUDIO_PATH.fullmatch(path):
                try:
                    from audio_graphy.services.receptions import (
                        verify_playback_grant,
                    )

                    claims = verify_playback_grant(
                        secret=self._playback_secret,
                        grant=grant,
                        expected_path=path,
                    )
                except ValueError:
                    return self._error_response(
                        InvalidTokenError("Invalid playback grant"),
                        request,
                    )
                request.state.user = AuthUser(
                    id=claims.subject_id,
                    name="",
                    email="",
                    role=claims.role,
                    tenant_id=claims.tenant_id,
                )
                request.state.tenant_id = claims.tenant_id
                if claims.role == "agent":
                    request.state.agent_filter = None
                return await call_next(request)

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
