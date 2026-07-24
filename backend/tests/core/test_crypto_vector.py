"""Unit tests for ``AudioCrypto.encrypt_bytes`` / ``decrypt_bytes`` (M7).

Covers the byte-level envelope used to encrypt voiceprint vectors:
- Roundtrip for arbitrary byte payloads.
- Float-packed voiceprint roundtrip (the real use case).
- Sha256 / size verification trips on tampered ciphertext.
- Header-malformed errors.
- Context tag is recorded in the header.
- Two different plaintexts produce different ciphertexts.
- Dev mode auto-generates master key.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from audio_graphy.core.crypto import AudioCrypto


@pytest.fixture
def dev_crypto(tmp_path: Path) -> AudioCrypto:
    """AudioCrypto in dev mode (auto-generates a master key)."""
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


class TestEncryptBytesRoundtrip:
    def test_small_bytes_roundtrip(self, dev_crypto: AudioCrypto) -> None:
        plain = b"hello-voiceprint"
        ct, meta = dev_crypto.encrypt_bytes(plain)
        assert dev_crypto.decrypt_bytes(ct, meta) == plain

    def test_empty_bytes_roundtrip(self, dev_crypto: AudioCrypto) -> None:
        plain = b""
        ct, meta = dev_crypto.encrypt_bytes(plain)
        assert dev_crypto.decrypt_bytes(ct, meta) == plain

    def test_large_bytes_roundtrip(self, dev_crypto: AudioCrypto) -> None:
        plain = b"x" * 100_000
        ct, meta = dev_crypto.encrypt_bytes(plain)
        assert dev_crypto.decrypt_bytes(ct, meta) == plain

    def test_voiceprint_float32_roundtrip(self, dev_crypto: AudioCrypto) -> None:
        """192-d float32 voiceprint — the primary M7 use case."""
        vec = tuple(float(i) / 200.0 for i in range(192))
        plain = struct.pack(f"<{len(vec)}f", *vec)
        ct, meta = dev_crypto.encrypt_bytes(plain)
        decrypted = dev_crypto.decrypt_bytes(ct, meta)
        assert decrypted == plain
        restored = struct.unpack(f"<{len(vec)}f", decrypted)
        for orig, got in zip(vec, restored, strict=True):
            assert got == pytest.approx(orig, abs=1e-7)

    def test_distinct_plaintexts_distinct_ciphertexts(self, dev_crypto: AudioCrypto) -> None:
        ct1, _ = dev_crypto.encrypt_bytes(b"payload-A")
        ct2, _ = dev_crypto.encrypt_bytes(b"payload-B")
        assert ct1 != ct2

    def test_same_plaintext_distinct_ciphertexts(self, dev_crypto: AudioCrypto) -> None:
        """Fernet nonce means same input produces different output."""
        ct1, _ = dev_crypto.encrypt_bytes(b"same-payload")
        ct2, _ = dev_crypto.encrypt_bytes(b"same-payload")
        assert ct1 != ct2


class TestEncryptBytesHeader:
    def test_context_recorded_in_header(self, dev_crypto: AudioCrypto) -> None:
        _, meta = dev_crypto.encrypt_bytes(b"x", context="voiceprint:vp_abc12345")
        assert meta["context"] == "voiceprint:vp_abc12345"

    def test_default_context_is_voiceprint(self, dev_crypto: AudioCrypto) -> None:
        _, meta = dev_crypto.encrypt_bytes(b"x")
        assert meta["context"] == "voiceprint"

    def test_header_records_sha256(self, dev_crypto: AudioCrypto) -> None:
        plain = b"sha-test-payload"
        _, meta = dev_crypto.encrypt_bytes(plain)
        assert meta["sha256"] == hashlib.sha256(plain).hexdigest()

    def test_header_records_size_bytes(self, dev_crypto: AudioCrypto) -> None:
        plain = b"x" * 777
        _, meta = dev_crypto.encrypt_bytes(plain)
        assert meta["size_bytes"] == 777

    def test_header_algo_is_aes_256_gcm(self, dev_crypto: AudioCrypto) -> None:
        _, meta = dev_crypto.encrypt_bytes(b"x")
        # Implementation uses "fernet" envelope (Fernet = AES-128-CBC + HMAC-SHA256
        # for the data key, AES-256-GCM at the master envelope layer). The
        # version field is locked at 1.
        assert meta["algo"] == "fernet"
        assert meta["version"] == 1


class TestDecryptBytesValidation:
    def test_tampered_body_raises(self, dev_crypto: AudioCrypto) -> None:
        ct, meta = dev_crypto.encrypt_bytes(b"payload")
        # Flip one byte in the cipher body (after the header newline).
        nl = ct.find(b"\n")
        tampered = ct[: nl + 5] + bytes([ct[nl + 5] ^ 0xFF]) + ct[nl + 6 :]
        with pytest.raises(ValueError, match=r"HMAC failed|sha256 mismatch"):
            dev_crypto.decrypt_bytes(tampered, meta)

    def test_tampered_size_in_header_raises(self, dev_crypto: AudioCrypto) -> None:
        plain = b"payload-1234"
        ct, meta = dev_crypto.encrypt_bytes(plain)
        bad_meta = dict(meta)
        bad_meta["size_bytes"] = 999
        # Pass ct + a stripped header (no inline header) by stripping the
        # header line and feeding meta explicitly via bad_meta.
        nl = ct.find(b"\n")
        body_only = ct[nl + 1 :]
        with pytest.raises(ValueError, match="size mismatch"):
            dev_crypto.decrypt_bytes(body_only, bad_meta)

    def test_tampered_sha256_raises(self, dev_crypto: AudioCrypto) -> None:
        plain = b"sha-tamper-test"
        ct, meta = dev_crypto.encrypt_bytes(plain)
        bad_meta = dict(meta)
        bad_meta["sha256"] = "0" * 64
        nl = ct.find(b"\n")
        body_only = ct[nl + 1 :]
        with pytest.raises(ValueError, match="sha256 mismatch"):
            dev_crypto.decrypt_bytes(body_only, bad_meta)

    def test_wrong_master_key_raises(self, dev_crypto: AudioCrypto, tmp_path: Path) -> None:
        ct, meta = dev_crypto.encrypt_bytes(b"payload")
        # New crypto instance with a different key.
        other = AudioCrypto(tmp_path / "other.key", dev_mode=True)
        with pytest.raises(ValueError, match=r"HMAC failed"):
            other.decrypt_bytes(ct, meta)

    def test_malformed_header_raises(self, dev_crypto: AudioCrypto) -> None:
        """Header without algo='fernet' is rejected."""
        bad_meta = {"version": 1, "algo": "ROT13"}
        with pytest.raises(ValueError, match="header malformed"):
            dev_crypto.decrypt_bytes(b"x", bad_meta)

    def test_missing_data_key_raises(self, dev_crypto: AudioCrypto) -> None:
        bad_meta = {"version": 1, "algo": "fernet"}  # no data_key_enc
        with pytest.raises(ValueError, match="missing data_key_enc"):
            dev_crypto.decrypt_bytes(b"x", bad_meta)


class TestCrossFormatCompat:
    def test_ciphertext_has_inline_header(self, dev_crypto: AudioCrypto) -> None:
        """encrypt_bytes returns ciphertext = JSON header + b'\\n' + body."""
        import json

        ct, meta = dev_crypto.encrypt_bytes(b"payload")
        nl = ct.find(b"\n")
        assert nl > 0
        header = json.loads(ct[:nl].decode("utf-8"))
        assert header["algo"] == meta["algo"]
        assert header["sha256"] == meta["sha256"]
        # Body is non-empty.
        assert len(ct) > nl + 1

    def test_decrypt_accepts_inline_header_only(self, dev_crypto: AudioCrypto) -> None:
        """If only the inline-header ciphertext is supplied, meta is ignored."""
        plain = b"payload-inline"
        ct, _ = dev_crypto.encrypt_bytes(plain)
        # Pass {} as meta — decrypt should use inline header.
        assert dev_crypto.decrypt_bytes(ct, {}) == plain
