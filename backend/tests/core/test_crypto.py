"""Unit tests for AudioCrypto — Fernet envelope encryption (PIPL §14.3).

Covers: roundtrip small/medium/large, wrong master key, missing key in
dev/prod, header corruption, sha256 mismatch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from audio_graphy.core.crypto import AudioCrypto


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    """Isolated dir for key + plaintext + ciphertext files."""
    return tmp_path


@pytest.fixture
def dev_crypto(key_dir: Path) -> AudioCrypto:
    """AudioCrypto with dev_mode=True; key auto-generated on first use."""
    return AudioCrypto(key_dir / "master.key", dev_mode=True)


def _write_plain(path: Path, size: int) -> bytes:
    """Write ``size`` deterministic bytes to ``path`` and return them."""
    payload = (b"AudioGraphy-crypto-test-" * ((size // 24) + 1))[:size]
    path.write_bytes(payload)
    return payload


def test_roundtrip_small(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """1 KB plaintext roundtrips with sha256 + size preserved."""
    plain = key_dir / "small.bin"
    payload = _write_plain(plain, 1024)
    cipher = key_dir / "small.enc"
    out = key_dir / "small.out"

    meta = dev_crypto.encrypt_file(plain, cipher)
    assert meta.size_bytes == 1024
    assert meta.sha256 == hashlib.sha256(payload).hexdigest()

    result = dev_crypto.decrypt_file(cipher, out)
    assert result.ok is True
    assert result.error is None
    assert result.size_bytes == 1024
    assert result.sha256 == meta.sha256
    assert out.read_bytes() == payload


def test_roundtrip_medium(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """100 KB plaintext roundtrips."""
    plain = key_dir / "med.bin"
    payload = _write_plain(plain, 100 * 1024)
    cipher = key_dir / "med.enc"
    out = key_dir / "med.out"

    dev_crypto.encrypt_file(plain, cipher)
    result = dev_crypto.decrypt_file(cipher, out)
    assert result.ok is True
    assert out.read_bytes() == payload


def test_roundtrip_large(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """1 MB plaintext roundtrips (validates no chunking regression)."""
    plain = key_dir / "large.bin"
    payload = _write_plain(plain, 1024 * 1024)
    cipher = key_dir / "large.enc"
    out = key_dir / "large.out"

    dev_crypto.encrypt_file(plain, cipher)
    result = dev_crypto.decrypt_file(cipher, out)
    assert result.ok is True
    assert out.read_bytes() == payload


def test_wrong_master_key_fails(key_dir: Path) -> None:
    """Decryption with a different master key returns hmac_failed."""
    key_a = key_dir / "ka"
    key_b = key_dir / "kb"
    enc_a = AudioCrypto(key_a, dev_mode=True)
    enc_b = AudioCrypto(key_b, dev_mode=True)

    plain = key_dir / "p.bin"
    _write_plain(plain, 256)
    cipher = key_dir / "p.enc"
    enc_a.encrypt_file(plain, cipher)

    result = enc_b.decrypt_file(cipher, key_dir / "p.out")
    assert result.ok is False
    assert result.error == "hmac_failed"
    assert result.plaintext_path is None


def test_missing_master_key_prod_raises(key_dir: Path) -> None:
    """In prod (dev_mode=False), missing master key raises FileNotFoundError."""
    crypto = AudioCrypto(key_dir / "absent.key", dev_mode=False)
    with pytest.raises(FileNotFoundError):
        crypto.encrypt_file(key_dir / "any.bin", key_dir / "any.enc")


def test_missing_master_key_dev_autogenerates(key_dir: Path) -> None:
    """In dev mode, missing master key is auto-generated; perms 0600."""
    key_path = key_dir / "auto.key"
    crypto = AudioCrypto(key_path, dev_mode=True)

    plain = key_dir / "p.bin"
    _write_plain(plain, 128)
    crypto.encrypt_file(plain, key_dir / "p.enc")

    assert key_path.exists()
    assert (key_path.stat().st_mode & 0o777) == 0o600


def test_corrupted_header_fails(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """Header JSON corruption → ok=False with error=header_corrupted."""
    plain = key_dir / "p.bin"
    _write_plain(plain, 256)
    cipher = key_dir / "p.enc"
    dev_crypto.encrypt_file(plain, cipher)

    # Truncate the header line so JSON parse fails.
    raw = cipher.read_bytes()
    nl = raw.find(b"\n")
    truncated = raw[: nl // 2] + b"\n" + raw[nl + 1 :]
    cipher.write_bytes(truncated)

    result = dev_crypto.decrypt_file(cipher, key_dir / "p.out")
    assert result.ok is False
    assert result.error == "header_corrupted"


def test_truncated_body_fails_hmac(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """Truncating the ciphertext body by 1 byte → hmac_failed."""
    plain = key_dir / "p.bin"
    _write_plain(plain, 1024)
    cipher = key_dir / "p.enc"
    dev_crypto.encrypt_file(plain, cipher)

    raw = cipher.read_bytes()
    cipher.write_bytes(raw[:-1])  # drop last byte

    result = dev_crypto.decrypt_file(cipher, key_dir / "p.out")
    assert result.ok is False
    assert result.error == "hmac_failed"


def test_sha256_mismatch_detected(dev_crypto: AudioCrypto, key_dir: Path) -> None:
    """Mutating header sha256 produces a mismatch error."""
    plain = key_dir / "p.bin"
    _write_plain(plain, 256)
    cipher = key_dir / "p.enc"
    dev_crypto.encrypt_file(plain, cipher)

    raw = cipher.read_bytes()
    nl = raw.find(b"\n")
    header = json.loads(raw[:nl].decode("utf-8"))
    header["sha256"] = "0" * 64  # bogus digest
    new_header = json.dumps(header).encode("utf-8")
    cipher.write_bytes(new_header + b"\n" + raw[nl + 1 :])

    result = dev_crypto.decrypt_file(cipher, key_dir / "p.out")
    assert result.ok is False
    assert result.error == "sha256_mismatch"
