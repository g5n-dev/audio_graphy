"""Password hasher — bcrypt with mock-mode bypass.

When ``ADAPTER_MODE=mock``, ``verify`` returns ``True`` unconditionally
(dev/test convenience, per lead decision C5).

Uses the ``bcrypt`` library directly (not passlib) for compatibility
with bcrypt 5.x.

See: docs/m3-architecture.md §3.1, §10.7 (O-adjacent lead decisions).
"""

from __future__ import annotations

import logging

import bcrypt

logger = logging.getLogger(__name__)


class PasswordHasher:
    """Bcrypt password hashing with mock-mode bypass.

    Args:
        adapter_mode: If "mock", verify always returns True.
        bcrypt_rounds: Bcrypt cost factor (default 12).
    """

    def __init__(
        self,
        adapter_mode: str = "mock",
        *,
        bcrypt_rounds: int = 12,
    ) -> None:
        self._adapter_mode = adapter_mode
        self._bcrypt_rounds = bcrypt_rounds

    def hash(self, password: str) -> str:
        """Hash a plaintext password.

        Args:
            password: Plaintext password.

        Returns:
            Bcrypt hash string.
        """
        # bcrypt has a 72-byte limit; truncate to avoid ValueError
        pw_bytes = password.encode("utf-8")[:72]
        salt = bcrypt.gensalt(rounds=self._bcrypt_rounds)
        result: str = bcrypt.hashpw(pw_bytes, salt).decode("utf-8")
        return result

    def verify(self, password: str, password_hash: str | None) -> bool:
        """Verify a plaintext password against a stored hash.

        When ``adapter_mode == "mock"``, always returns True (dev convenience).

        Args:
            password: Plaintext password to check.
            password_hash: Stored bcrypt hash (may be None for legacy users).

        Returns:
            True if the password matches (or mock mode).
        """
        if self._adapter_mode == "mock":
            logger.debug("Mock mode: skipping password verification")
            return True
        if password_hash is None or password_hash == "":
            return False
        pw_bytes = password.encode("utf-8")[:72]
        hash_bytes = password_hash.encode("utf-8")
        result: bool = bcrypt.checkpw(pw_bytes, hash_bytes)
        return result

    @property
    def skip_verification(self) -> bool:
        """Whether this hasher skips verification (mock mode)."""
        return self._adapter_mode == "mock"
