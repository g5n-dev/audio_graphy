"""Coverage gap-fill tests for AudioCrypto.

Targets the uncovered branches in decrypt_file + _get_fernet:
- read_failed path (ciphertext missing)
- no newline in ciphertext (header_corrupted)
- algo mismatch in header
- data_key_enc field is not a str
- non-InvalidToken exception during data_key decrypt
- size_mismatch path
- raw 32-byte master key path
- malformed master key (raises ValueError)
- non-0600 master key perms (logs warning)
- chmod failure on dev key generation
- rotate_master_key raises NotImplementedError
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from audio_graphy.core.crypto import AudioCrypto


def test_decrypt_missing_file_returns_read_failed(tmp_path: Path) -> None:
    """decrypt_file on a missing ciphertext → ok=False, error=read_failed."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    result = crypto.decrypt_file(tmp_path / "absent.enc", tmp_path / "out.bin")
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("read_failed")


def test_decrypt_no_newline_returns_header_corrupted(tmp_path: Path) -> None:
    """A ciphertext with no embedded newline → header_corrupted."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    bad = tmp_path / "nonewline.enc"
    bad.write_bytes(b"just-bytes-no-newline")
    result = crypto.decrypt_file(bad, tmp_path / "out.bin")
    assert result.ok is False
    assert result.error == "header_corrupted"


def test_decrypt_algo_mismatch_returns_header_corrupted(tmp_path: Path) -> None:
    """Header with algo != 'fernet' → header_corrupted."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"x" * 32)
    cipher = tmp_path / "p.enc"
    crypto.encrypt_file(plain, cipher)

    raw = cipher.read_bytes()
    nl = raw.find(b"\n")
    header = json.loads(raw[:nl].decode("utf-8"))
    header["algo"] = "not-fernet"
    new_header = json.dumps(header).encode("utf-8")
    cipher.write_bytes(new_header + b"\n" + raw[nl + 1 :])

    result = crypto.decrypt_file(cipher, tmp_path / "out.bin")
    assert result.ok is False
    assert result.error == "header_corrupted"


def test_decrypt_data_key_not_str_returns_header_corrupted(tmp_path: Path) -> None:
    """Header with data_key_enc field as non-str → header_corrupted."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"x" * 32)
    cipher = tmp_path / "p.enc"
    crypto.encrypt_file(plain, cipher)

    raw = cipher.read_bytes()
    nl = raw.find(b"\n")
    header = json.loads(raw[:nl].decode("utf-8"))
    header["data_key_enc"] = 12345  # not a str
    new_header = json.dumps(header).encode("utf-8")
    cipher.write_bytes(new_header + b"\n" + raw[nl + 1 :])

    result = crypto.decrypt_file(cipher, tmp_path / "out.bin")
    assert result.ok is False
    assert result.error == "header_corrupted"


def test_decrypt_size_mismatch_detected(tmp_path: Path) -> None:
    """Header size_bytes mismatching actual plaintext size → size_mismatch."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"x" * 100)
    cipher = tmp_path / "p.enc"
    crypto.encrypt_file(plain, cipher)

    raw = cipher.read_bytes()
    nl = raw.find(b"\n")
    header = json.loads(raw[:nl].decode("utf-8"))
    header["size_bytes"] = 999  # wrong size
    new_header = json.dumps(header).encode("utf-8")
    cipher.write_bytes(new_header + b"\n" + raw[nl + 1 :])

    result = crypto.decrypt_file(cipher, tmp_path / "out.bin")
    assert result.ok is False
    assert result.error == "size_mismatch"


def test_raw_32_byte_master_key_accepted(tmp_path: Path) -> None:
    """A raw 32-byte master key file is accepted (urlsafe-base64 re-encoded)."""
    key_path = tmp_path / "raw32.key"
    raw_bytes = b"k" * 32
    key_path.write_bytes(raw_bytes)
    # Set 0600 to silence the perm warning.
    os.chmod(key_path, 0o600)

    crypto = AudioCrypto(key_path)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"plaintext-payload" * 10)
    cipher = tmp_path / "p.enc"
    meta = crypto.encrypt_file(plain, cipher)
    assert meta.size_bytes == plain.stat().st_size

    out = tmp_path / "p.out"
    result = crypto.decrypt_file(cipher, out)
    assert result.ok is True
    assert out.read_bytes() == plain.read_bytes()


def test_malformed_master_key_raises_value_error(tmp_path: Path) -> None:
    """A non-base64, non-32-byte master key raises ValueError."""
    key_path = tmp_path / "bad.key"
    key_path.write_bytes(b"definitely not a valid key")
    os.chmod(key_path, 0o600)

    crypto = AudioCrypto(key_path)
    with pytest.raises(ValueError, match="Malformed master key"):
        crypto.encrypt_file(tmp_path / "any.bin", tmp_path / "any.enc")


def test_non_0600_master_key_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A master key with perms other than 0600 logs a WARNING (but still works)."""
    key_path = tmp_path / "perms.key"
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    os.chmod(key_path, 0o644)  # world-readable → warning

    crypto = AudioCrypto(key_path)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello" * 10)
    cipher = tmp_path / "p.enc"

    with caplog.at_level("WARNING"):
        crypto.encrypt_file(plain, cipher)

    assert any("expected 0600" in r.message for r in caplog.records)


def test_rotate_master_key_raises_not_implemented(tmp_path: Path) -> None:
    """rotate_master_key stub raises NotImplementedError (M7+ lands)."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    with pytest.raises(NotImplementedError, match="M7\\+"):
        crypto.rotate_master_key(tmp_path / "old.key", tmp_path / "new.key")


def test_dev_mode_chmod_failure_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If chmod fails in dev key generation, a warning is logged but encryption works."""

    def _raise_chmod(_path: Path, _mode: int) -> None:
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", _raise_chmod)

    key_path = tmp_path / "chmod_fail.key"
    crypto = AudioCrypto(key_path, dev_mode=True)

    plain = tmp_path / "p.bin"
    plain.write_bytes(b"data" * 8)

    with caplog.at_level("WARNING"):
        meta = crypto.encrypt_file(plain, tmp_path / "p.enc")

    assert meta.size_bytes == 32
    # The chmod failure should have been logged.
    assert any("chmod master key" in r.message for r in caplog.records)


def test_encrypt_idempotent_metadata_freshness(tmp_path: Path) -> None:
    """Two encryptions of the same plaintext produce different data_key_id."""
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"some-data" * 50)

    meta1 = crypto.encrypt_file(plain, tmp_path / "p1.enc")
    meta2 = crypto.encrypt_file(plain, tmp_path / "p2.enc")

    # Each encryption uses a fresh data key → different data_key_id.
    assert meta1.data_key_id != meta2.data_key_id
    # But both share the master_key_id and sha256.
    assert meta1.master_key_id == meta2.master_key_id
    assert meta1.sha256 == meta2.sha256 == hashlib.sha256(plain.read_bytes()).hexdigest()
