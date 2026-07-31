"""Authenticated, bounded encryption tests for persisted LLM cache values."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto


def _crypto(tmp_path: Path, *, max_plaintext_bytes: int = 1024) -> LLMCacheCrypto:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(Fernet.generate_key())
    return LLMCacheCrypto(
        key_path,
        max_plaintext_bytes=max_plaintext_bytes,
    )


def test_cache_crypto_roundtrip_and_metadata(tmp_path: Path) -> None:
    crypto = _crypto(tmp_path)
    plaintext = '{"text":"你好，AudioGraphy"}'.encode()

    encrypted = crypto.encrypt(
        tenant_id="tenant-a",
        namespace="final_answer",
        recipe_sha256="a" * 64,
        plaintext=plaintext,
    )

    assert plaintext not in encrypted.blob
    assert encrypted.metadata["algorithm"] == "AES-256-GCM"
    assert encrypted.metadata["kdf"] == "HKDF-SHA256"
    assert encrypted.metadata["compression"] == "zlib"
    assert (
        crypto.decrypt(
            tenant_id="tenant-a",
            namespace="final_answer",
            recipe_sha256="a" * 64,
            encrypted=encrypted,
        )
        == plaintext
    )


@pytest.mark.parametrize(
    ("tenant_id", "namespace", "recipe_sha256"),
    [
        ("tenant-b", "final_answer", "a" * 64),
        ("tenant-a", "keyword_extract", "a" * 64),
        ("tenant-a", "final_answer", "b" * 64),
    ],
)
def test_cache_crypto_aad_binds_identity(
    tmp_path: Path,
    tenant_id: str,
    namespace: str,
    recipe_sha256: str,
) -> None:
    crypto = _crypto(tmp_path)
    encrypted = crypto.encrypt(
        tenant_id="tenant-a",
        namespace="final_answer",
        recipe_sha256="a" * 64,
        plaintext=b"secret",
    )

    with pytest.raises(ValueError, match="authentication"):
        crypto.decrypt(
            tenant_id=tenant_id,
            namespace=namespace,
            recipe_sha256=recipe_sha256,
            encrypted=encrypted,
        )


def test_cache_crypto_rejects_tampering(tmp_path: Path) -> None:
    crypto = _crypto(tmp_path)
    encrypted = crypto.encrypt(
        tenant_id="tenant-a",
        namespace="tag_extract",
        recipe_sha256="c" * 64,
        plaintext=b"validated output",
    )
    tampered = encrypted.with_blob(encrypted.blob[:-1] + bytes([encrypted.blob[-1] ^ 1]))

    with pytest.raises(ValueError, match="authentication"):
        crypto.decrypt(
            tenant_id="tenant-a",
            namespace="tag_extract",
            recipe_sha256="c" * 64,
            encrypted=tampered,
        )


def test_cache_crypto_accepts_raw_32_byte_master_key(tmp_path: Path) -> None:
    key_path = tmp_path / "raw.key"
    # Raw binary key material may legitimately begin with whitespace.
    key_path.write_bytes(b" " + b"k" * 31)
    crypto = LLMCacheCrypto(key_path, max_plaintext_bytes=1024)

    encrypted = crypto.encrypt(
        tenant_id="tenant-a",
        namespace="keyword_extract",
        recipe_sha256="d" * 64,
        plaintext=b"keywords",
    )

    assert (
        crypto.decrypt(
            tenant_id="tenant-a",
            namespace="keyword_extract",
            recipe_sha256="d" * 64,
            encrypted=encrypted,
        )
        == b"keywords"
    )


def test_cache_crypto_enforces_encrypt_and_decompress_limits(tmp_path: Path) -> None:
    permissive = _crypto(tmp_path, max_plaintext_bytes=4096)
    encrypted = permissive.encrypt(
        tenant_id="tenant-a",
        namespace="keyword_extract",
        recipe_sha256="e" * 64,
        plaintext=b"x" * 2048,
    )
    bounded = LLMCacheCrypto(
        tmp_path / "master.key",
        max_plaintext_bytes=32,
    )

    with pytest.raises(ValueError, match="limit"):
        bounded.decrypt(
            tenant_id="tenant-a",
            namespace="keyword_extract",
            recipe_sha256="e" * 64,
            encrypted=encrypted,
        )
    with pytest.raises(ValueError, match="limit"):
        bounded.encrypt(
            tenant_id="tenant-a",
            namespace="keyword_extract",
            recipe_sha256="f" * 64,
            plaintext=b"x" * 33,
        )


def test_cache_crypto_rejects_malformed_master_key(tmp_path: Path) -> None:
    key_path = tmp_path / "bad.key"
    key_path.write_bytes(b"not-a-valid-master-key")

    with pytest.raises(ValueError, match="master key"):
        LLMCacheCrypto(key_path).validate_master_key()
