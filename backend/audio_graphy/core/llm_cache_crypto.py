"""Domain-separated authenticated encryption for LLM cache payloads.

The cache reuses the deployment master-key material but never reuses the
audio-encryption key directly. HKDF-SHA256 derives a dedicated AES-256-GCM
key, and associated data binds every ciphertext to its tenant, namespace,
recipe hash, and format version.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MAGIC: Final = b"AGLC"
_FORMAT_VERSION: Final = 1
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_HEADER_BYTES: Final = len(_MAGIC) + 1 + _NONCE_BYTES
_KDF_SALT: Final = b"audiography/llm-cache/hkdf-salt/v1"
_KDF_INFO: Final = b"audiography/llm-cache/aesgcm/v1"
_DEFAULT_MAX_PLAINTEXT_BYTES: Final = 16 * 1024 * 1024
_COMPRESSED_OVERHEAD_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class EncryptedCachePayload:
    """Self-contained ciphertext plus non-secret algorithm metadata."""

    blob: bytes
    metadata: dict[str, str | int]

    def with_blob(self, blob: bytes) -> EncryptedCachePayload:
        """Return the same metadata paired with a replacement blob."""

        return replace(self, blob=blob)


class LLMCacheCrypto:
    """Compress, encrypt, authenticate, and boundedly decrypt cache values."""

    def __init__(
        self,
        master_key_path: Path,
        *,
        max_plaintext_bytes: int = _DEFAULT_MAX_PLAINTEXT_BYTES,
        max_compressed_bytes: int | None = None,
    ) -> None:
        if max_plaintext_bytes < 1:
            raise ValueError("max_plaintext_bytes must be positive")
        compressed_limit = (
            max_plaintext_bytes + _COMPRESSED_OVERHEAD_BYTES
            if max_compressed_bytes is None
            else max_compressed_bytes
        )
        if compressed_limit < 1:
            raise ValueError("max_compressed_bytes must be positive")
        self._master_key_path = Path(master_key_path)
        self._max_plaintext_bytes = max_plaintext_bytes
        self._max_compressed_bytes = compressed_limit
        self._aesgcm: AESGCM | None = None
        self._key_id: str | None = None

    @property
    def max_plaintext_bytes(self) -> int:
        return self._max_plaintext_bytes

    def validate_master_key(self) -> None:
        """Eagerly load and validate master-key material."""

        self._get_aesgcm()

    def encrypt(
        self,
        *,
        tenant_id: str,
        namespace: str,
        recipe_sha256: str,
        plaintext: bytes,
    ) -> EncryptedCachePayload:
        """Return a self-contained AES-GCM ciphertext."""

        self._validate_identity(tenant_id, namespace, recipe_sha256)
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if len(plaintext) > self._max_plaintext_bytes:
            raise ValueError("LLM cache plaintext exceeds configured limit")

        compressed = zlib.compress(plaintext)
        if len(compressed) > self._max_compressed_bytes:
            raise ValueError("LLM cache compressed payload exceeds configured limit")

        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad = self._aad(tenant_id, namespace, recipe_sha256)
        ciphertext = self._get_aesgcm().encrypt(nonce, compressed, aad)
        blob = _MAGIC + bytes([_FORMAT_VERSION]) + nonce + ciphertext
        return EncryptedCachePayload(
            blob=blob,
            metadata={
                "version": _FORMAT_VERSION,
                "algorithm": "AES-256-GCM",
                "kdf": "HKDF-SHA256",
                "compression": "zlib",
                "key_id": self._key_id or "",
            },
        )

    def decrypt(
        self,
        *,
        tenant_id: str,
        namespace: str,
        recipe_sha256: str,
        encrypted: EncryptedCachePayload,
    ) -> bytes:
        """Authenticate, decrypt, and boundedly decompress one payload."""

        self._validate_identity(tenant_id, namespace, recipe_sha256)
        self._validate_metadata(encrypted.metadata)
        blob = encrypted.blob
        if (
            len(blob) < _HEADER_BYTES + _TAG_BYTES
            or blob[: len(_MAGIC)] != _MAGIC
            or blob[len(_MAGIC)] != _FORMAT_VERSION
        ):
            raise ValueError("LLM cache encrypted payload is malformed")
        if len(blob) > _HEADER_BYTES + _TAG_BYTES + self._max_compressed_bytes:
            raise ValueError("LLM cache encrypted payload exceeds configured limit")

        nonce_start = len(_MAGIC) + 1
        nonce = blob[nonce_start : nonce_start + _NONCE_BYTES]
        ciphertext = blob[nonce_start + _NONCE_BYTES :]
        try:
            compressed = self._get_aesgcm().decrypt(
                nonce,
                ciphertext,
                self._aad(tenant_id, namespace, recipe_sha256),
            )
        except InvalidTag as exc:
            raise ValueError("LLM cache authentication failed") from exc
        if len(compressed) > self._max_compressed_bytes:
            raise ValueError("LLM cache compressed payload exceeds configured limit")
        return self._bounded_decompress(compressed)

    def _bounded_decompress(self, compressed: bytes) -> bytes:
        decompressor = zlib.decompressobj()
        try:
            plaintext = decompressor.decompress(
                compressed,
                self._max_plaintext_bytes + 1,
            )
            if len(plaintext) > self._max_plaintext_bytes or decompressor.unconsumed_tail:
                raise ValueError("LLM cache decompressed payload exceeds configured limit")
            remaining = self._max_plaintext_bytes - len(plaintext) + 1
            plaintext += decompressor.flush(remaining)
        except zlib.error as exc:
            raise ValueError("LLM cache compressed payload is malformed") from exc
        if (
            len(plaintext) > self._max_plaintext_bytes
            or not decompressor.eof
            or decompressor.unused_data
        ):
            raise ValueError("LLM cache decompressed payload exceeds configured limit")
        return plaintext

    def _get_aesgcm(self) -> AESGCM:
        if self._aesgcm is not None:
            return self._aesgcm
        if not self._master_key_path.is_file():
            raise FileNotFoundError(f"LLM cache master key not found: {self._master_key_path}")
        key_material = self._master_key_path.read_bytes()
        try:
            if len(key_material) == 32:
                master_key = key_material
            else:
                encoded = key_material.strip()
                master_key = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if len(master_key) != 32:
                raise ValueError("master key must decode to exactly 32 bytes")
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Malformed LLM cache master key: {exc}") from exc

        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_KDF_SALT,
            info=_KDF_INFO,
        ).derive(master_key)
        self._aesgcm = AESGCM(derived_key)
        self._key_id = hashlib.sha256(master_key).hexdigest()[:16]
        return self._aesgcm

    @staticmethod
    def _aad(tenant_id: str, namespace: str, recipe_sha256: str) -> bytes:
        return json.dumps(
            {
                "namespace": namespace,
                "recipe_sha256": recipe_sha256,
                "tenant_id": tenant_id,
                "version": _FORMAT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _validate_identity(tenant_id: str, namespace: str, recipe_sha256: str) -> None:
        if not isinstance(tenant_id, str) or not 1 <= len(tenant_id) <= 64:
            raise ValueError("tenant_id must contain 1 to 64 characters")
        if not isinstance(namespace, str) or not 1 <= len(namespace) <= 64:
            raise ValueError("namespace must contain 1 to 64 characters")
        if (
            not isinstance(recipe_sha256, str)
            or len(recipe_sha256) != 64
            or any(character not in "0123456789abcdef" for character in recipe_sha256)
        ):
            raise ValueError("recipe_sha256 must be a lowercase SHA-256 hex digest")

    @staticmethod
    def _validate_metadata(metadata: Mapping[str, str | int]) -> None:
        required = {
            "version": _FORMAT_VERSION,
            "algorithm": "AES-256-GCM",
            "kdf": "HKDF-SHA256",
            "compression": "zlib",
        }
        if any(metadata.get(key) != value for key, value in required.items()):
            raise ValueError("LLM cache encryption metadata is malformed")


__all__ = ["EncryptedCachePayload", "LLMCacheCrypto"]
