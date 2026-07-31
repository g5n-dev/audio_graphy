"""Password hasher — bcrypt.

Verification is unconditional: there is no mode in which a wrong password is
accepted. An earlier revision short-circuited ``verify`` when
``ADAPTER_MODE=mock``, which coupled authentication to the model-backend
selector — and since ``ADAPTER_MODE`` defaults to ``mock`` and is explicitly
documented as *not* driving adapter resolution, a deployment that switched the
per-adapter modes to ``real`` still accepted any password. Seed a user with
``scripts/bootstrap_admin.py`` instead.

Uses the ``bcrypt`` library directly (not passlib) for compatibility
with bcrypt 5.x.

See: docs/m3-architecture.md §3.1, §10.7 (O-adjacent lead decisions).
"""

from __future__ import annotations

import logging

import bcrypt

logger = logging.getLogger(__name__)

# bcrypt rejects secrets longer than 72 bytes; longer inputs are truncated
# consistently on both hash and verify so the pair stays symmetric.
_BCRYPT_MAX_BYTES = 72


class PasswordHasher:
    """Bcrypt password hashing.

    Args:
        bcrypt_rounds: Bcrypt cost factor (default 12).
    """

    def __init__(self, *, bcrypt_rounds: int = 12) -> None:
        self._bcrypt_rounds = bcrypt_rounds

    def hash(self, password: str) -> str:
        """Hash a plaintext password.

        Args:
            password: Plaintext password.

        Returns:
            Bcrypt hash string.
        """
        pw_bytes = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        salt = bcrypt.gensalt(rounds=self._bcrypt_rounds)
        result: str = bcrypt.hashpw(pw_bytes, salt).decode("utf-8")
        return result

    def verify(self, password: str, password_hash: str | None) -> bool:
        """Verify a plaintext password against a stored hash.

        Args:
            password: Plaintext password to check.
            password_hash: Stored bcrypt hash (may be None for legacy users).

        Returns:
            True only if the password matches the stored hash.
        """
        if not password_hash:
            return False
        pw_bytes = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        hash_bytes = password_hash.encode("utf-8")
        try:
            result: bool = bcrypt.checkpw(pw_bytes, hash_bytes)
        except ValueError:
            # Stored value is not a valid bcrypt hash (e.g. a legacy placeholder).
            # Treat it as a failed verification rather than a 500.
            logger.warning("Stored password hash is not a valid bcrypt digest")
            return False
        return result
