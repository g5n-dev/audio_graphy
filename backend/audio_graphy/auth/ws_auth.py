"""WebSocket JWT authentication helper (M8 P0-4 / §6.1.2).

JWT is passed via the ``?token=`` query parameter (browsers don't support
custom headers on WebSocket). The token is verified using the same
``JWTManager`` as REST; only ``type="access"`` tokens are accepted.

On failure, raises ``WebSocketException(code=4001)`` which FastAPI
translates into a WS close with code 4001 + reason ``"auth failed"``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketException

from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.errors import InvalidTokenError, TokenExpiredError

logger = logging.getLogger(__name__)

# Custom close code (per architecture §6.1.2) — in the 4000-4999 application range.
WS_AUTH_FAILED_CODE: int = 4001


@dataclass(frozen=True, slots=True)
class WSAuthUser:
    """Identity extracted from the JWT for a WS connection.

    Attributes:
        user_id: Authenticated user id (``sub`` claim).
        tenant_id: Tenant scope (``tid`` claim).
        role: RBAC role string.
    """

    user_id: int
    tenant_id: str
    role: str


async def verify_ws_token(token: str, jwt_manager: JWTManager) -> WSAuthUser:
    """Verify JWT from the ``?token=`` query parameter.

    Args:
        token: Raw JWT string.
        jwt_manager: Application JWTManager.

    Returns:
        WSAuthUser on success.

    Raises:
        WebSocketException: code 4001 on any verification failure.
    """
    if not token:
        logger.warning("WS auth failed: empty token")
        raise WebSocketException(
            code=WS_AUTH_FAILED_CODE,
            reason="missing token",
        )
    try:
        payload = jwt_manager.verify_access_token(token)
    except TokenExpiredError as exc:
        logger.warning("WS auth failed: token expired")
        raise WebSocketException(
            code=WS_AUTH_FAILED_CODE,
            reason="token expired",
        ) from exc
    except InvalidTokenError as exc:
        logger.warning("WS auth failed: invalid token")
        raise WebSocketException(
            code=WS_AUTH_FAILED_CODE,
            reason="invalid token",
        ) from exc
    return WSAuthUser(
        user_id=payload.sub,
        tenant_id=payload.tid,
        role=payload.role,
    )


async def ws_auth_dependency(ws: WebSocket, jwt_manager: JWTManager) -> WSAuthUser:
    """FastAPI-style dependency for WS auth.

    Usage::

        @router.websocket("/ws/stream")
        async def ws_stream(
            ws: WebSocket,
            token: str = Query(...),
            settings: Settings = Depends(get_settings),
        ):
            user = await ws_auth_dependency_with_token(token, settings)

    This helper expects to be called with the token already extracted from
    query params. The reason we don't use ``Depends(...)`` directly is that
    FastAPI WS dependency injection for query params requires the function
    to be declared as ``Depends``; the explicit call site is clearer for
    error handling.
    """
    # The actual token retrieval is done by the caller (Query param).
    # This function is kept for symmetry / future hook-based extraction.
    raise NotImplementedError("use verify_ws_token directly")
