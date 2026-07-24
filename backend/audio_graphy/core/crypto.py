"""AudioCrypto — bounded, authenticated envelope encryption for audio at rest.

PIPL §14.3 implementation: each audio file is encrypted with a per-file
data key (Fernet.generate_key), and the data key is wrapped by the master
Fernet key loaded from ``AUDIOGRAPHY_MASTER_KEY_PATH`` (default
``/run/secrets/audiography_master.key``, 0600 permissions).

New files use a streaming v2 layout::

    +-------------------------------------------------+
    | JSON header line (UTF-8) + "\\n"                 |
    +-------------------------------------------------+
    | uint32 length + authenticated header token       |
    +-------------------------------------------------+
    | uint32 length + indexed Fernet chunk token ...   |
    +-------------------------------------------------+
    | uint32 length + authenticated footer token       |
    +-------------------------------------------------+

The footer authenticates chunk count, plaintext byte count, and SHA-256; the
opening record authenticates the exact header bytes. This detects truncation,
reordering, duplication, header changes, and body tampering without loading a
whole audio file into memory. The legacy v1 single-Fernet layout remains
readable under a strict memory/size cap.

Header JSON keys: ``version``, ``algo``, ``master_key_id``,
``data_key_id``, ``data_key_enc`` (base64-Fernet), ``nonce`` (base64),
``size_bytes`` (plaintext), ``sha256`` (plaintext), ``chunk_size``, and
``chunk_count``.

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
import stat
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_LEGACY_HEADER_VERSION = 1
_LEGACY_HEADER_ALGO = "fernet"
_CHUNKED_HEADER_VERSION = 2
_CHUNKED_HEADER_ALGO = "fernet-chunked"
_MASTER_KEY_ID_DEFAULT = "master"
_NONCE_BYTES = 16
_DEFAULT_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_LEGACY_PLAINTEXT_BYTES = 256 * 1024 * 1024
_MIN_CHUNK_SIZE_BYTES = 1024
_MAX_CHUNK_SIZE_BYTES = 16 * 1024 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_MAX_CHUNKS = 1_000_000
_RECORD_LENGTH = struct.Struct(">I")
_RECORD_INDEX = struct.Struct(">Q")
_FOOTER_FIELDS = struct.Struct(">QQ32s")
_HEADER_RECORD = b"H"
_DATA_RECORD = b"D"
_FOOTER_RECORD = b"F"


class _CipherFormatError(ValueError):
    """The ciphertext framing or authenticated metadata is malformed."""


class _CipherAuthenticationError(ValueError):
    """A wrapped key or bounded ciphertext record failed authentication."""


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
        chunk_size_bytes: int = _DEFAULT_CHUNK_SIZE_BYTES,
        max_plaintext_bytes: int = _DEFAULT_MAX_PLAINTEXT_BYTES,
    ) -> None:
        if not _MIN_CHUNK_SIZE_BYTES <= chunk_size_bytes <= _MAX_CHUNK_SIZE_BYTES:
            raise ValueError(
                f"chunk_size_bytes must be in [{_MIN_CHUNK_SIZE_BYTES}, {_MAX_CHUNK_SIZE_BYTES}]"
            )
        if max_plaintext_bytes <= 0:
            raise ValueError("max_plaintext_bytes must be positive")
        self._master_key_path = Path(master_key_path)
        self._dev_mode = dev_mode
        self._master_key_id = master_key_id
        self._chunk_size_bytes = chunk_size_bytes
        self._max_plaintext_bytes = max_plaintext_bytes
        self._fernet: Fernet | None = None  # lazy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def validate_master_key(self) -> None:
        """Eagerly load and validate the configured master key.

        Startup wiring calls this so a missing or malformed production key
        cannot silently leave the ingestion path unencrypted.
        """
        self._get_fernet()

    def encrypt_file(
        self,
        plaintext_path: Path,
        ciphertext_path: Path,
    ) -> EncryptionMetadata:
        """Stream-encrypt one file into the authenticated v2 chunk format.

        Only one bounded chunk and its Fernet token are resident at a time.
        The destination is fsynced and atomically replaced after a second-pass
        source identity/hash check, so cancellation or failure never publishes
        a partial ciphertext.
        """
        master_fernet = self._get_fernet()
        source = Path(plaintext_path)
        out_path = Path(ciphertext_path)
        source_identity, size_bytes, sha256 = self._inspect_plaintext(source)
        if out_path.resolve(strict=False) == source.resolve(strict=True):
            raise ValueError("ciphertext_path must not replace plaintext_path")
        self._validate_existing_target(out_path)

        chunk_count = (size_bytes + self._chunk_size_bytes - 1) // self._chunk_size_bytes
        if chunk_count > _MAX_CHUNKS:
            raise ValueError(f"plaintext requires more than {_MAX_CHUNKS} chunks")
        data_key = Fernet.generate_key()
        data_key_fernet = Fernet(data_key)
        data_key_enc = master_fernet.encrypt(data_key).decode("ascii")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        data_key_id = secrets.token_hex(8)

        header = {
            "version": _CHUNKED_HEADER_VERSION,
            "algo": _CHUNKED_HEADER_ALGO,
            "master_key_id": self._master_key_id,
            "data_key_id": data_key_id,
            "data_key_enc": data_key_enc,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "size_bytes": size_bytes,
            "sha256": sha256,
            "chunk_size": self._chunk_size_bytes,
            "chunk_count": chunk_count,
        }
        header_line = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(header_line) > _MAX_HEADER_BYTES:
            raise ValueError("ciphertext header exceeds the format limit")

        temporary_path, output = self._open_atomic_output(out_path)
        try:
            with output, source.open("rb") as plaintext:
                output.write(header_line)
                output.write(b"\n")
                self._write_record(
                    output,
                    data_key_fernet.encrypt(_HEADER_RECORD + hashlib.sha256(header_line).digest()),
                )
                actual_digest = hashlib.sha256()
                actual_size = 0
                actual_chunks = 0
                while chunk := plaintext.read(self._chunk_size_bytes):
                    if len(chunk) > self._chunk_size_bytes:
                        raise RuntimeError("plaintext reader exceeded the configured chunk size")
                    actual_digest.update(chunk)
                    actual_size += len(chunk)
                    payload = _DATA_RECORD + _RECORD_INDEX.pack(actual_chunks) + chunk
                    self._write_record(output, data_key_fernet.encrypt(payload))
                    actual_chunks += 1

                actual_sha = actual_digest.hexdigest()
                if (
                    actual_size != size_bytes
                    or actual_chunks != chunk_count
                    or actual_sha != sha256
                    or self._source_identity(source) != source_identity
                ):
                    raise ValueError("plaintext changed during encryption")
                footer = _FOOTER_RECORD + _FOOTER_FIELDS.pack(
                    actual_chunks,
                    actual_size,
                    bytes.fromhex(actual_sha),
                )
                self._write_record(output, data_key_fernet.encrypt(footer))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, out_path)
            self._fsync_directory(out_path.parent)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

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
        """Decrypt a v2 chunked file or a bounded legacy v1 Fernet envelope."""
        started = time.perf_counter()
        in_path = Path(ciphertext_path)
        out_path = Path(plaintext_path)
        try:
            encrypted_stat = os.stat(in_path, follow_symlinks=False)
        except OSError as exc:
            return self._fail(out_path, started, f"read_failed: {exc!s}")
        if not stat.S_ISREG(encrypted_stat.st_mode):
            return self._fail(out_path, started, "header_corrupted")
        self._validate_existing_target(out_path)
        try:
            with in_path.open("rb") as encrypted:
                header_line = encrypted.readline(_MAX_HEADER_BYTES + 1)
                if not header_line.endswith(b"\n") or len(header_line) > _MAX_HEADER_BYTES:
                    return self._fail(out_path, started, "header_corrupted")
                encoded_header = header_line[:-1]
                header_object = json.loads(encoded_header.decode("utf-8"))
                if not isinstance(header_object, dict):
                    return self._fail(out_path, started, "header_corrupted")
                header = header_object
                algo = header.get("algo")
                if algo == _CHUNKED_HEADER_ALGO:
                    return self._decrypt_chunked(
                        encrypted,
                        encoded_header,
                        header,
                        out_path,
                        started,
                    )
                if algo == _LEGACY_HEADER_ALGO:
                    return self._decrypt_legacy(
                        encrypted,
                        header,
                        encrypted_stat.st_size,
                        out_path,
                        started,
                    )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self._fail(out_path, started, "header_corrupted")
        return self._fail(out_path, started, "header_corrupted")

    def _decrypt_chunked(
        self,
        encrypted: BinaryIO,
        encoded_header: bytes,
        header: dict[str, Any],
        out_path: Path,
        started: float,
    ) -> DecryptionResult:
        try:
            expected_size, expected_sha, chunk_size, chunk_count = self._validate_chunked_header(
                header
            )
            data_fernet = Fernet(self._unwrap_data_key(header))
            max_token_bytes = chunk_size * 2 + 4096
            opening = self._decrypt_record(
                self._read_record(encrypted, max_token_bytes=max_token_bytes),
                data_fernet,
            )
            if len(opening) != 33 or not opening.startswith(_HEADER_RECORD):
                raise _CipherFormatError("invalid authenticated header record")
            authenticated_header_digest = opening[1:]
        except _CipherAuthenticationError:
            return self._fail(out_path, started, "hmac_failed")
        except (_CipherFormatError, ValueError):
            return self._fail(out_path, started, "header_corrupted")

        temporary_path: Path | None = None
        try:
            temporary_path, output = self._open_atomic_output(out_path)
            actual_digest = hashlib.sha256()
            actual_size = 0
            with output:
                for expected_index in range(chunk_count):
                    token = self._read_record(
                        encrypted,
                        max_token_bytes=max_token_bytes,
                    )
                    payload = self._decrypt_record(token, data_fernet)
                    if (
                        len(payload) < 9
                        or not payload.startswith(_DATA_RECORD)
                        or _RECORD_INDEX.unpack(payload[1:9])[0] != expected_index
                    ):
                        raise _CipherFormatError("invalid or out-of-order data record")
                    chunk = payload[9:]
                    if not chunk or len(chunk) > chunk_size:
                        raise _CipherFormatError("invalid plaintext chunk length")
                    if expected_index < chunk_count - 1 and len(chunk) != chunk_size:
                        raise _CipherFormatError("short non-final plaintext chunk")
                    actual_size += len(chunk)
                    if actual_size > self._max_plaintext_bytes:
                        raise _CipherFormatError("plaintext exceeds configured size limit")
                    actual_digest.update(chunk)
                    output.write(chunk)

                footer_payload = self._decrypt_record(
                    self._read_record(encrypted, max_token_bytes=max_token_bytes),
                    data_fernet,
                )
                if len(footer_payload) != 1 + _FOOTER_FIELDS.size or not footer_payload.startswith(
                    _FOOTER_RECORD
                ):
                    raise _CipherFormatError("invalid authenticated footer record")
                footer_chunks, footer_size, footer_sha = _FOOTER_FIELDS.unpack(footer_payload[1:])
                if encrypted.read(1):
                    raise _CipherFormatError("trailing ciphertext records")

                actual_sha = actual_digest.hexdigest()
                if (
                    footer_chunks != chunk_count
                    or footer_size != actual_size
                    or footer_sha.hex() != actual_sha
                ):
                    raise _CipherFormatError("authenticated footer does not match plaintext")
                if actual_size != expected_size:
                    temporary_path.unlink(missing_ok=True)
                    return self._fail(out_path, started, "size_mismatch")
                if actual_sha != expected_sha:
                    temporary_path.unlink(missing_ok=True)
                    return self._fail(out_path, started, "sha256_mismatch")
                if authenticated_header_digest != hashlib.sha256(encoded_header).digest():
                    raise _CipherFormatError("authenticated header digest mismatch")
                output.flush()
                os.fsync(output.fileno())

            os.replace(temporary_path, out_path)
            self._fsync_directory(out_path.parent)
            duration_ms = (time.perf_counter() - started) * 1000.0
            return DecryptionResult(
                plaintext_path=out_path,
                size_bytes=actual_size,
                sha256=actual_sha,
                duration_ms=duration_ms,
                ok=True,
                error=None,
            )
        except _CipherAuthenticationError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return self._fail(out_path, started, "hmac_failed")
        except _CipherFormatError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            error = "hmac_failed" if "truncated" in str(exc) else "format_corrupted"
            return self._fail(out_path, started, error)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return self._fail(out_path, started, f"write_failed: {exc!s}")

    def _decrypt_legacy(
        self,
        encrypted: BinaryIO,
        header: dict[str, Any],
        ciphertext_size: int,
        out_path: Path,
        started: float,
    ) -> DecryptionResult:
        """Read the legacy single-token body under an explicit memory cap."""
        legacy_limit = min(self._max_plaintext_bytes, _MAX_LEGACY_PLAINTEXT_BYTES)
        max_ciphertext_bytes = legacy_limit * 2 + _MAX_HEADER_BYTES
        if ciphertext_size > max_ciphertext_bytes:
            return self._fail(out_path, started, "resource_limit_exceeded")
        try:
            expected_size, expected_sha = self._validate_legacy_header(header, legacy_limit)
            data_fernet = Fernet(self._unwrap_data_key(header))
            cipher_body = encrypted.read(max_ciphertext_bytes + 1)
            if len(cipher_body) > max_ciphertext_bytes:
                return self._fail(out_path, started, "resource_limit_exceeded")
            plaintext = data_fernet.decrypt(cipher_body)
        except InvalidToken:
            return self._fail(out_path, started, "hmac_failed")
        except _CipherAuthenticationError:
            return self._fail(out_path, started, "hmac_failed")
        except (_CipherFormatError, ValueError):
            return self._fail(out_path, started, "header_corrupted")
        except Exception as exc:
            return self._fail(out_path, started, f"decrypt_failed: {exc!s}")

        if len(plaintext) != expected_size:
            return self._fail(out_path, started, "size_mismatch")
        actual_sha = hashlib.sha256(plaintext).hexdigest()
        if actual_sha != expected_sha:
            return self._fail(out_path, started, "sha256_mismatch")
        temporary_path: Path | None = None
        try:
            temporary_path, output = self._open_atomic_output(out_path)
            with output:
                output.write(plaintext)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, out_path)
            self._fsync_directory(out_path.parent)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return self._fail(out_path, started, f"write_failed: {exc!s}")
        duration_ms = (time.perf_counter() - started) * 1000.0
        return DecryptionResult(
            plaintext_path=out_path,
            size_bytes=len(plaintext),
            sha256=actual_sha,
            duration_ms=duration_ms,
            ok=True,
            error=None,
        )

    def _validate_chunked_header(
        self,
        header: dict[str, Any],
    ) -> tuple[int, str, int, int]:
        if (
            header.get("version") != _CHUNKED_HEADER_VERSION
            or header.get("algo") != _CHUNKED_HEADER_ALGO
        ):
            raise _CipherFormatError("unsupported chunked envelope version")
        expected_size = self._required_bounded_int(
            header,
            "size_bytes",
            minimum=0,
            maximum=self._max_plaintext_bytes,
        )
        expected_sha = self._required_sha256(header)
        chunk_size = self._required_bounded_int(
            header,
            "chunk_size",
            minimum=_MIN_CHUNK_SIZE_BYTES,
            maximum=_MAX_CHUNK_SIZE_BYTES,
        )
        chunk_count = self._required_bounded_int(
            header,
            "chunk_count",
            minimum=0,
            maximum=_MAX_CHUNKS,
        )
        calculated_chunks = (expected_size + chunk_size - 1) // chunk_size
        if chunk_count != calculated_chunks:
            raise _CipherFormatError("chunk_count does not match declared size")
        return expected_size, expected_sha, chunk_size, chunk_count

    def _validate_legacy_header(
        self,
        header: dict[str, Any],
        legacy_limit: int,
    ) -> tuple[int, str]:
        if (
            header.get("version") != _LEGACY_HEADER_VERSION
            or header.get("algo") != _LEGACY_HEADER_ALGO
        ):
            raise _CipherFormatError("unsupported legacy envelope version")
        expected_size = self._required_bounded_int(
            header,
            "size_bytes",
            minimum=0,
            maximum=legacy_limit,
        )
        return expected_size, self._required_sha256(header)

    def _unwrap_data_key(self, header: dict[str, Any]) -> bytes:
        data_key_enc = header.get("data_key_enc")
        if not isinstance(data_key_enc, str) or not 1 <= len(data_key_enc) <= 4096:
            raise _CipherFormatError("data_key_enc is missing or invalid")
        try:
            encoded_key = data_key_enc.encode("ascii")
        except UnicodeEncodeError as exc:
            raise _CipherFormatError("data_key_enc is not ASCII") from exc
        try:
            return self._get_fernet().decrypt(encoded_key)
        except InvalidToken as exc:
            raise _CipherAuthenticationError("wrapped data key authentication failed") from exc
        except Exception as exc:
            raise _CipherAuthenticationError("wrapped data key decryption failed") from exc

    @staticmethod
    def _required_bounded_int(
        header: dict[str, Any],
        key: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = header.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _CipherFormatError(f"{key} must be an integer")
        if not minimum <= value <= maximum:
            raise _CipherFormatError(f"{key} is outside the supported range")
        return value

    @staticmethod
    def _required_sha256(header: dict[str, Any]) -> str:
        value = header.get("sha256")
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise _CipherFormatError("sha256 is malformed")
        return value

    @staticmethod
    def _write_record(output: BinaryIO, token: bytes) -> None:
        if not token or len(token) > 0xFFFFFFFF:
            raise ValueError("encrypted record length is invalid")
        output.write(_RECORD_LENGTH.pack(len(token)))
        output.write(token)

    @staticmethod
    def _read_record(encrypted: BinaryIO, *, max_token_bytes: int) -> bytes:
        encoded_length = encrypted.read(_RECORD_LENGTH.size)
        if len(encoded_length) != _RECORD_LENGTH.size:
            raise _CipherFormatError("truncated ciphertext record length")
        token_length = _RECORD_LENGTH.unpack(encoded_length)[0]
        if not 80 <= token_length <= max_token_bytes:
            raise _CipherFormatError("ciphertext record length is outside the supported range")
        token = encrypted.read(token_length)
        if len(token) != token_length:
            raise _CipherFormatError("truncated ciphertext record")
        return token

    @staticmethod
    def _decrypt_record(token: bytes, data_fernet: Fernet) -> bytes:
        try:
            return data_fernet.decrypt(token)
        except InvalidToken as exc:
            raise _CipherAuthenticationError("ciphertext record authentication failed") from exc

    def _inspect_plaintext(
        self,
        source: Path,
    ) -> tuple[tuple[int, int, int, int], int, str]:
        identity = self._source_identity(source)
        size_bytes = identity[2]
        if size_bytes > self._max_plaintext_bytes:
            raise ValueError(
                f"plaintext size {size_bytes} exceeds configured limit {self._max_plaintext_bytes}"
            )
        digest = hashlib.sha256()
        actual_size = 0
        with source.open("rb") as plaintext:
            while chunk := plaintext.read(self._chunk_size_bytes):
                actual_size += len(chunk)
                digest.update(chunk)
        final_identity = self._source_identity(source)
        if final_identity != identity or actual_size != size_bytes:
            raise ValueError("plaintext changed while being inspected")
        return identity, size_bytes, digest.hexdigest()

    @staticmethod
    def _source_identity(source: Path) -> tuple[int, int, int, int]:
        source_stat = os.stat(source, follow_symlinks=False)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("plaintext_path must name a regular file")
        return (
            source_stat.st_dev,
            source_stat.st_ino,
            source_stat.st_size,
            source_stat.st_mtime_ns,
        )

    @staticmethod
    def _validate_existing_target(target: Path) -> None:
        if target.is_symlink():
            raise ValueError("output path must not be a symbolic link")
        if target.exists():
            target_stat = os.stat(target, follow_symlinks=False)
            if not stat.S_ISREG(target_stat.st_mode):
                raise ValueError("output path must name a regular file")

    @staticmethod
    def _open_atomic_output(target: Path) -> tuple[Path, BinaryIO]:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(raw_temporary_path)
        try:
            return temporary_path, os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            # File data is already durable; some platforms/filesystems do not
            # support directory fsync.
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def rotate_master_key(self, old_path: Path, new_path: Path) -> int:
        """Re-encrypt all data keys under a new master. M6 STUB.

        Raises:
            NotImplementedError: always. M7+ will scan recordings and
                re-wrap each ``data_key_enc``.
        """
        raise NotImplementedError("Master key rotation lands in M7+ (see docs/m6-pipl.md).")

    # ------------------------------------------------------------------
    # M7 — byte-level encrypt / decrypt (voiceprint vectors)
    # ------------------------------------------------------------------
    def encrypt_bytes(
        self,
        plaintext: bytes,
        *,
        context: str = "voiceprint",
    ) -> tuple[bytes, dict[str, Any]]:
        """Encrypt an in-memory byte payload (e.g. voiceprint vector).

        Uses the same envelope as ``encrypt_file`` but skips the on-disk
        write — the ciphertext + header are returned directly so the caller
        can store them in a JSON column (e.g. ``vectors_voiceprint.encryption_meta``)
        alongside the binary ciphertext.

        Args:
            plaintext: Raw bytes to encrypt (e.g. struct-packed floats).
            context: Free-form context tag recorded in the audit header
                (default ``"voiceprint"``).

        Returns:
            ``(ciphertext_bytes, encryption_meta_json_dict)``.
        """
        fernet = self._get_fernet()
        sha256 = hashlib.sha256(plaintext).hexdigest()
        size_bytes = len(plaintext)

        data_key = Fernet.generate_key()
        data_key_fernet = Fernet(data_key)
        data_key_enc = fernet.encrypt(data_key).decode("ascii")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        data_key_id = secrets.token_hex(8)

        cipher_body = data_key_fernet.encrypt(plaintext)
        header = {
            "version": _LEGACY_HEADER_VERSION,
            "algo": _LEGACY_HEADER_ALGO,
            "context": context,
            "master_key_id": self._master_key_id,
            "data_key_id": data_key_id,
            "data_key_enc": data_key_enc,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "size_bytes": size_bytes,
            "sha256": sha256,
        }
        # Prepend a header line so the on-disk format stays identical to
        # encrypt_file (a future admin tool can dump vectors_voiceprint rows
        # to disk for offline decryption). For the in-memory return, both
        # the header line + cipher body go into ciphertext_bytes.
        header_line = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
        ciphertext = header_line.encode("utf-8") + b"\n" + cipher_body
        return ciphertext, header

    def decrypt_bytes(
        self,
        ciphertext: bytes,
        encryption_meta: dict[str, Any],
    ) -> bytes:
        """Decrypt bytes produced by ``encrypt_bytes``.

        Accepts either:
            - The combined ``ciphertext`` (header line + cipher body) returned
              by ``encrypt_bytes``; ``encryption_meta`` is ignored.
            - A raw Fernet body where the header is passed separately via
              ``encryption_meta`` (e.g. when the caller split them).

        Verifies SHA-256 + size and raises ``ValueError`` on mismatch.

        Args:
            ciphertext: Encrypted bytes.
            encryption_meta: Header dict from ``encrypt_bytes`` return.

        Returns:
            Plaintext bytes.
        """
        # Determine if ciphertext has an inline header line.
        newline_idx = ciphertext.find(b"\n")
        if newline_idx >= 0:
            try:
                header = json.loads(ciphertext[:newline_idx].decode("utf-8"))
                body = ciphertext[newline_idx + 1 :]
            except (UnicodeDecodeError, json.JSONDecodeError):
                header = encryption_meta
                body = ciphertext
        else:
            header = encryption_meta
            body = ciphertext

        if not isinstance(header, dict) or header.get("algo") != _LEGACY_HEADER_ALGO:
            raise ValueError("decrypt_bytes: header malformed or algo mismatch")

        data_key_enc = header.get("data_key_enc")
        if not isinstance(data_key_enc, str):
            raise ValueError("decrypt_bytes: missing data_key_enc")

        fernet = self._get_fernet()
        try:
            data_key = fernet.decrypt(data_key_enc.encode("ascii"))
        except InvalidToken as exc:
            raise ValueError("decrypt_bytes: HMAC failed on data key") from exc
        try:
            plaintext = Fernet(data_key).decrypt(body)
        except InvalidToken as exc:
            raise ValueError("decrypt_bytes: HMAC failed on body") from exc

        expected_size = header.get("size_bytes")
        if isinstance(expected_size, int) and len(plaintext) != expected_size:
            raise ValueError(f"decrypt_bytes: size mismatch ({len(plaintext)} != {expected_size})")
        expected_sha = header.get("sha256")
        if isinstance(expected_sha, str):
            actual_sha = hashlib.sha256(plaintext).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError("decrypt_bytes: sha256 mismatch")
        return plaintext

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
                    raise ValueError(f"Master key must decode to 32 bytes, got {len(decoded)}")
                fernet_key = key_bytes
            self._fernet = Fernet(fernet_key)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Malformed master key at {self._master_key_path}: {exc}") from exc

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
            "DEV MODE: auto-generating master key at %s — DO NOT use in production.",
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
