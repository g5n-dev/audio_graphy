"""JWT manager — HS256 access + refresh token signing and verification.

Tokens carry ``sub`` (user_id), ``tid`` (tenant_id), ``role``, ``exp``, ``type``.

See: docs/m3-architecture.md §3.1, docs/m3-prd.md AUTH-01.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from audio_graphy.errors import InvalidRefreshTokenError, InvalidTokenError, TokenExpiredError


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Decoded JWT payload fields.

    Attributes:
        sub: User ID (subject).
        tid: Tenant ID.
        role: RBAC role.
        exp: Expiry timestamp (epoch seconds).
        type: Token type ("access" or "refresh").
    """

    sub: int
    tid: str
    role: str
    exp: int
    type: str


class JWTManager:
    """JWT signing and verification (HS256).

    Args:
        secret: HMAC secret key.
        algorithm: Signing algorithm (default HS256).
        exp_hours: Access token TTL in hours.
        refresh_exp_hours: Refresh token TTL in hours.
    """

    def __init__(
        self,
        secret: str,
        *,
        algorithm: str = "HS256",
        exp_hours: int = 12,
        refresh_exp_hours: int | None = None,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._exp_hours = exp_hours
        self._refresh_exp_hours = (
            refresh_exp_hours if refresh_exp_hours is not None else exp_hours * 7
        )

    @property
    def expires_in(self) -> int:
        """Access token TTL in seconds."""
        return self._exp_hours * 3600

    def create_access_token(self, user_id: int, tenant_id: str, role: str) -> str:
        """Create a signed JWT access token.

        Args:
            user_id: User ID.
            tenant_id: Tenant ID.
            role: RBAC role string.

        Returns:
            Encoded JWT string.
        """
        now = datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "tid": tenant_id,
            "role": role,
            "type": "access",
            "exp": now + timedelta(hours=self._exp_hours),
            "iat": now,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_refresh_token(self, user_id: int, tenant_id: str, role: str) -> str:
        """Create a signed JWT refresh token.

        Args:
            user_id: User ID.
            tenant_id: Tenant ID.
            role: RBAC role string.

        Returns:
            Encoded JWT string.
        """
        now = datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "tid": tenant_id,
            "role": role,
            "type": "refresh",
            "exp": now + timedelta(hours=self._refresh_exp_hours),
            "iat": now,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def decode_token(self, token: str) -> dict[str, object]:
        """Decode a JWT token without type checking.

        Args:
            token: Encoded JWT string.

        Returns:
            Decoded payload dict.

        Raises:
            TokenExpiredError: If the token has expired.
            InvalidTokenError: If the token is invalid.
        """
        try:
            return jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("Token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise InvalidTokenError(f"Invalid token: {exc}") from exc

    def verify_access_token(self, token: str) -> TokenPayload:
        """Verify an access token and return its payload.

        Args:
            token: Encoded JWT access token.

        Returns:
            TokenPayload with decoded fields.

        Raises:
            TokenExpiredError: If expired.
            InvalidTokenError: If invalid or wrong type.
        """
        payload = self.decode_token(token)
        token_type = str(payload.get("type", ""))
        if token_type != "access":
            raise InvalidTokenError(f"Expected access token, got type='{token_type}'")
        return self._payload_from_dict(payload)

    def verify_refresh_token(self, token: str) -> TokenPayload:
        """Verify a refresh token and return its payload.

        Args:
            token: Encoded JWT refresh token.

        Returns:
            TokenPayload with decoded fields.

        Raises:
            InvalidRefreshTokenError: If invalid, expired, or wrong type.
        """
        try:
            payload = self.decode_token(token)
        except TokenExpiredError as exc:
            raise InvalidRefreshTokenError("Refresh token has expired") from exc
        except InvalidTokenError as exc:
            raise InvalidRefreshTokenError(f"Invalid refresh token: {exc}") from exc

        token_type = str(payload.get("type", ""))
        if token_type != "refresh":
            raise InvalidRefreshTokenError(f"Expected refresh token, got type='{token_type}'")
        return self._payload_from_dict(payload)

    @staticmethod
    def _payload_from_dict(payload: dict[str, object]) -> TokenPayload:
        """Build TokenPayload from decoded JWT dict."""
        sub_raw = payload.get("sub", "0")
        sub_int = int(str(sub_raw))
        exp_raw = payload.get("exp", 0)
        exp_int = int(str(exp_raw))
        return TokenPayload(
            sub=sub_int,
            tid=str(payload.get("tid", "")),
            role=str(payload.get("role", "")),
            exp=exp_int,
            type=str(payload.get("type", "")),
        )
