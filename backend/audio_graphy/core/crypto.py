"""AudioCrypto — Fernet envelope encryption for audio files at rest.

PIPL §14.3 implementation: each audio file is encrypted with a per-file
data key (Fernet.generate_key), and the data key is wrapped by the master
Fernet key loaded from ``AUDIOGRAPHY_MASTER_KEY_PATH`` (default
``/run/secrets/audiography_master.key``, 0600 permissions).

Ciphertext file layout (M6 v1)::

    +-------------------------------------------------+
    | JSON header line (UTF-8) + "\\n"                 |
    +-------------------------------------------------+
    | Fernet token bytes (AES-128-CBC + HMAC-SHA256)  |
    +-------------------------------------------------+

Header JSON keys: ``version``, ``algo``, ``master_key_id``,
``data_key_id``, ``data_key_enc`` (base64-Fernet), ``nonce`` (base64),
``size_bytes`` (plaintext), ``sha256`` (plaintext).

Why envelope (not single Fernet over the whole file)? M7+ will support
master-key rotation: only data keys need re-wrapping, not 10MB+ audio
bodies. The envelope format preserves that upgrade path.

See: docs/m6-architecture.md §3.1 (PIPL §14.3), docs/m6-prd.md §4.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_HEADER_VERSION = 1
_HEADER_ALGO = "fernet"
_MASTER_KEY_ID_DEFAULT = "master"
_NONCE_BYTES = 16


@dataclass(frozen=True, slots=True)
class EncryptionMetadata:
    """AudioCrypto.encrypt_file return value; persisted to DB.

    Attributes:
        file_path: Path to the ciphertext file written.
        master_key_id: Logical identifier of the master key used.
        data_key_id: Random hex identifying this file's data key.
        size_bytes: Plaintext size in bytes (also in header).
        sha256: SHA-256 hex of the plaintext (also in header).
    """

    file_path: str
    master_key_id: str
    data_key_id: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DecryptionResult:
    """AudioCrypto.decrypt_file return value.

    Attributes:
        plaintext_path: Path where plaintext was written (None on failure).
        size_bytes: Plaintext byte count (0 on failure).
        sha256: Recovered plaintext SHA-256 (None on failure).
        duration_ms: Wall-clock decrypt duration.
        ok: True iff HMAC + size + sha256 all verified.
        error: Failure code (None when ok=True).
    """

    plaintext_path: Path | None
    size_bytes: int
    sha256: str | None
    duration_ms: float
    ok: bool
    error: str | None


class AudioCrypto:
    """Fernet envelope encryption for audio files at rest.

    The master key file may be either:
        - 32 raw bytes, OR
        - 44-char urlsafe-base64 string (``Fernet.generate_key()`` output).

    File permissions are checked on first load; a non-0600 mode logs a
    WARNING but does not fail (callers can fail-fast in their own setup).

    Args:
        master_key_path: Path to master key file (0600 recommended).
        dev_mode: If True and the key file is missing, auto-generate one
            with a loud WARNING. Production callers MUST leave this False.
        master_key_id: Logical id recorded in headers / audit logs; useful
            when M7+ introduces key rotation.

    Raises:
        FileNotFoundError: Key missing and ``dev_mode=False``.
        ValueError: Key content malformed.
    """

    def __init__(
        self,
        master_key_path: Path,
        *,
        dev_mode: bool = False,
        master_key_id: str = _MASTER_KEY_ID_DEFAULT,
    ) -> None:
        self._master_key_path = Path(master_key_path)
        self._dev_mode = dev_mode
        self._master_key_id = master_key_id
        self._fernet: Fernet | None = None  # lazy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def encrypt_file(
        self,
        plaintext_path: Path,
        ciphertext_path: Path,
    ) -> EncryptionMetadata:
        """Encrypt ``plaintext_path`` → ``ciphertext_path``.

        Steps:
            1. Read plaintext bytes (single read; M6 targets ≤100MB).
            2. Compute SHA-256 of plaintext.
            3. Generate a per-file data key (Fernet.generate_key()).
            4. Encrypt the data key with the master Fernet.
            5. Encrypt the plaintext with the data-key Fernet.
            6. Write JSON header line + ciphertext bytes to disk.

        Args:
            plaintext_path: Source plaintext file (must exist).
            ciphertext_path: Destination encrypted file (will be created
                or overwritten).

        Returns:
            EncryptionMetadata for DB persistence / audit logs.
        """
        fernet = self._get_fernet()
        plaintext = Path(plaintext_path).read_bytes()
        sha256 = hashlib.sha256(plaintext).hexdigest()
        size_bytes = len(plaintext)

        data_key = Fernet.generate_key()
        data_key_fernet = Fernet(data_key)
        data_key_enc = fernet.encrypt(data_key).decode("ascii")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        data_key_id = secrets.token_hex(8)

        cipher_body = data_key_fernet.encrypt(plaintext)

        header = {
            "version": _HEADER_VERSION,
            "algo": _HEADER_ALGO,
            "master_key_id": self._master_key_id,
            "data_key_id": data_key_id,
            "data_key_enc": data_key_enc,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "size_bytes": size_bytes,
            "sha256": sha256,
        }
        header_line = json.dumps(header, ensure_ascii=False, separators=(",", ":"))

        out_path = Path(ciphertext_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as f:
            f.write(header_line.encode("utf-8"))
            f.write(b"\n")
            f.write(cipher_body)

        return EncryptionMetadata(
            file_path=str(out_path),
            master_key_id=self._master_key_id,
            data_key_id=data_key_id,
            size_bytes=size_bytes,
            sha256=sha256,
        )

    def decrypt_file(
        self,
        ciphertext_path: Path,
        plaintext_path: Path,
    ) -> DecryptionResult:
        """Decrypt ``ciphertext_path`` → ``plaintext_path``.

        Verifies:
            - header parses cleanly (else ``error="header_corrupted"``)
            - Fernet HMAC validates (else ``error="hmac_failed"``)
            - plaintext size + SHA-256 match header
              (else ``error="sha256_mismatch"`` or ``"size_mismatch"``)

        Args:
            ciphertext_path: Encrypted file written by encrypt_file.
            plaintext_path: Destination plaintext file.

        Returns:
            DecryptionResult. ``ok=False`` carries an error code; no
            exception is raised for ordinary decryption failures.
        """
        started = time.perf_counter()
        in_path = Path(ciphertext_path)
        out_path = Path(plaintext_path)

        try:
            raw = in_path.read_bytes()
        except OSError as exc:
            return self._fail(out_path, started, f"read_failed: {exc!s}")

        newline_idx = raw.find(b"\n")
        if newline_idx < 0:
            return self._fail(out_path, started, "header_corrupted")

        try:
            header_bytes = raw[:newline_idx].decode("utf-8")
            header = json.loads(header_bytes)
            cipher_body = raw[newline_idx + 1 :]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self._fail(out_path, started, "header_corrupted")

        if not isinstance(header, dict) or header.get("algo") != _HEADER_ALGO:
            return self._fail(out_path, started, "header_corrupted")

        data_key_enc = header.get("data_key_enc")
        expected_size = header.get("size_bytes")
        expected_sha = header.get("sha256")
        if not isinstance(data_key_enc, str):
            return self._fail(out_path, started, "header_corrupted")

        fernet = self._get_fernet()

        try:
            data_key = fernet.decrypt(data_key_enc.encode("ascii"))
        except InvalidToken:
            return self._fail(out_path, started, "hmac_failed")
        except Exception as exc:
            return self._fail(out_path, started, f"hmac_failed: {exc!s}")

        try:
            plaintext = Fernet(data_key).decrypt(cipher_body)
        except InvalidToken:
            return self._fail(out_path, started, "hmac_failed")
        except Exception as exc:
            return self._fail(out_path, started, f"decrypt_failed: {exc!s}")

        if expected_size is not None and len(plaintext) != expected_size:
            return self._fail(out_path, started, "size_mismatch")
        actual_sha = hashlib.sha256(plaintext).hexdigest()
        if isinstance(expected_sha, str) and actual_sha != expected_sha:
            return self._fail(out_path, started, "sha256_mismatch")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(plaintext)
        duration_ms = (time.perf_counter() - started) * 1000.0
        return DecryptionResult(
            plaintext_path=out_path,
            size_bytes=len(plaintext),
            sha256=actual_sha,
            duration_ms=duration_ms,
            ok=True,
            error=None,
        )

    def rotate_master_key(self, old_path: Path, new_path: Path) -> int:
        """Re-encrypt all data keys under a new master. M6 STUB.

        Raises:
            NotImplementedError: always. M7+ will scan recordings and
                re-wrap each ``data_key_enc``.
        """
        raise NotImplementedError(
            "Master key rotation lands in M7+ (see docs/m6-pipl.md)."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_fernet(self) -> Fernet:
        """Lazy-load master Fernet, auto-generating in dev mode."""
        if self._fernet is not None:
            return self._fernet

        if not self._master_key_path.exists():
            if self._dev_mode:
                self._generate_dev_key()
            else:
                raise FileNotFoundError(
                    f"Master key not found at {self._master_key_path}; "
                    "set AUDIOGRAPHY_MASTER_KEY_PATH or run with dev_mode=True"
                )

        key_bytes = self._master_key_path.read_bytes().strip()
        try:
            if len(key_bytes) == 32:
                # Accept raw 32-byte keys by urlsafe-base64 re-encoding.
                fernet_key = base64.urlsafe_b64encode(key_bytes)
            else:
                # Validate by round-tripping urlsafe-base64 → 32 bytes.
                decoded = base64.urlsafe_b64decode(key_bytes)
                if len(decoded) != 32:
                    raise ValueError(
                        f"Master key must decode to 32 bytes, got {len(decoded)}"
                    )
                fernet_key = key_bytes
            self._fernet = Fernet(fernet_key)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                f"Malformed master key at {self._master_key_path}: {exc}"
            ) from exc

        # Verify file permissions (warn-only; caller enforces in prod).
        try:
            mode = self._master_key_path.stat().st_mode & 0o777
            if mode != 0o600:
                logger.warning(
                    "Master key file %s has permissions %o; expected 0600",
                    self._master_key_path,
                    mode,
                )
        except OSError:
            pass

        return self._fernet

    def _generate_dev_key(self) -> None:
        """Generate a new master key for development. Loud warning."""
        logger.warning(
            "DEV MODE: auto-generating master key at %s — "
            "DO NOT use in production.",
            self._master_key_path,
        )
        self._master_key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._master_key_path.write_bytes(key)
        try:
            os.chmod(self._master_key_path, 0o600)
        except OSError as exc:
            logger.warning("Failed to chmod master key: %s", exc)

    @staticmethod
    def _fail(
        out_path: Path,
        started: float,
        error: str,
    ) -> DecryptionResult:
        """Build a failure DecryptionResult."""
        duration_ms = (time.perf_counter() - started) * 1000.0
        return DecryptionResult(
            plaintext_path=None,
            size_bytes=0,
            sha256=None,
            duration_ms=duration_ms,
            ok=False,
            error=error,
        )


__all__ = [
    "AudioCrypto",
    "DecryptionResult",
    "EncryptionMetadata",
]
