"""Resource and integrity boundaries for chunked audio-file encryption."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from audio_graphy.core.crypto import AudioCrypto


def _write_repeated(path: Path, *, size: int, chunk_size: int = 64 * 1024) -> str:
    digest = hashlib.sha256()
    remaining = size
    block = b"audio-graphy-streaming-test-" * 4096
    with path.open("wb") as output:
        while remaining:
            chunk = block[: min(len(block), chunk_size, remaining)]
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def test_large_roundtrip_never_uses_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = tmp_path / "large.wav"
    cipher = tmp_path / "large.wav.enc"
    restored = tmp_path / "large.out"
    expected_sha = _write_repeated(plain, size=8 * 1024 * 1024 + 17)
    crypto = AudioCrypto(
        tmp_path / "master.key",
        dev_mode=True,
        chunk_size_bytes=64 * 1024,
    )
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path in {plain, cipher, restored}:
            raise AssertionError("audio file must not be loaded with Path.read_bytes")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    metadata = crypto.encrypt_file(plain, cipher)
    result = crypto.decrypt_file(cipher, restored)

    assert metadata.sha256 == expected_sha
    assert result.ok is True
    assert result.size_bytes == plain.stat().st_size
    assert _sha256_file(restored) == expected_sha
    with cipher.open("rb") as encrypted:
        header = json.loads(encrypted.readline().decode("utf-8"))
    assert header["version"] == 2
    assert header["algo"] == "fernet-chunked"
    assert header["chunk_count"] > 1


def test_decrypt_file_accepts_legacy_single_fernet_envelope(tmp_path: Path) -> None:
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    plaintext = b"legacy-audio" * 1024
    legacy_ciphertext, _ = crypto.encrypt_bytes(plaintext, context="legacy-file")
    cipher = tmp_path / "legacy.enc"
    output = tmp_path / "legacy.wav"
    cipher.write_bytes(legacy_ciphertext)

    result = crypto.decrypt_file(cipher, output)

    assert result.ok is True
    assert output.read_bytes() == plaintext


@pytest.mark.parametrize("mutation", ("truncate", "tamper"))
def test_chunked_ciphertext_corruption_is_rejected_without_publishing_plaintext(
    tmp_path: Path,
    mutation: str,
) -> None:
    crypto = AudioCrypto(
        tmp_path / "master.key",
        dev_mode=True,
        chunk_size_bytes=1024,
    )
    plain = tmp_path / "source.wav"
    cipher = tmp_path / "source.wav.enc"
    output = tmp_path / "restored.wav"
    _write_repeated(plain, size=12_345)
    crypto.encrypt_file(plain, cipher)
    output.write_bytes(b"previous-valid-output")

    if mutation == "truncate":
        with cipher.open("r+b") as encrypted:
            encrypted.truncate(cipher.stat().st_size - 7)
    else:
        with cipher.open("r+b") as encrypted:
            encrypted.seek(cipher.stat().st_size // 2)
            original = encrypted.read(1)
            encrypted.seek(-1, 1)
            encrypted.write(bytes([original[0] ^ 0x01]))

    result = crypto.decrypt_file(cipher, output)

    assert result.ok is False
    assert result.error in {"hmac_failed", "format_corrupted"}
    assert output.read_bytes() == b"previous-valid-output"
    assert not list(tmp_path.glob(".restored.wav.*.tmp"))


def test_encrypt_rejects_source_over_configured_limit_without_touching_target(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "source.wav"
    cipher = tmp_path / "source.wav.enc"
    plain.write_bytes(b"x" * 1025)
    cipher.write_bytes(b"previous-valid-ciphertext")
    crypto = AudioCrypto(
        tmp_path / "master.key",
        dev_mode=True,
        max_plaintext_bytes=1024,
    )

    with pytest.raises(ValueError, match="exceeds"):
        crypto.encrypt_file(plain, cipher)

    assert cipher.read_bytes() == b"previous-valid-ciphertext"


def test_header_chunk_count_tampering_is_rejected(tmp_path: Path) -> None:
    crypto = AudioCrypto(
        tmp_path / "master.key",
        dev_mode=True,
        chunk_size_bytes=1024,
    )
    plain = tmp_path / "source.wav"
    cipher = tmp_path / "source.wav.enc"
    output = tmp_path / "restored.wav"
    plain.write_bytes(b"x" * 4097)
    crypto.encrypt_file(plain, cipher)

    with cipher.open("rb") as encrypted:
        header_line = encrypted.readline()
        body = encrypted.read()
    header = json.loads(header_line)
    header["chunk_count"] += 1
    with cipher.open("wb") as encrypted:
        encrypted.write(json.dumps(header, separators=(",", ":")).encode())
        encrypted.write(b"\n")
        encrypted.write(body)

    result = crypto.decrypt_file(cipher, output)

    assert result.ok is False
    assert not output.exists()
