"""Tests for the one-shot Compose master-key initializer."""

from __future__ import annotations

import base64
import os

import pytest

from scripts.init_master_key import initialize_key


def test_initialize_key_creates_private_valid_key_and_is_idempotent(tmp_path) -> None:
    key_path = tmp_path / "secrets" / "audio.key"

    initialize_key(key_path, owner_uid=os.getuid(), owner_gid=os.getgid())
    original = key_path.read_bytes()

    assert len(base64.urlsafe_b64decode(original)) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600

    initialize_key(key_path, owner_uid=os.getuid(), owner_gid=os.getgid())
    assert key_path.read_bytes() == original


def test_initialize_key_rejects_malformed_existing_file(tmp_path) -> None:
    key_path = tmp_path / "audio.key"
    key_path.write_text("not-a-key", encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed"):
        initialize_key(key_path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_initialize_key_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("not-a-key", encoding="utf-8")
    key_path = tmp_path / "audio.key"
    key_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        initialize_key(key_path, owner_uid=os.getuid(), owner_gid=os.getgid())


def test_state_fingerprint_rejects_missing_or_replaced_key(tmp_path) -> None:
    key_path = tmp_path / "secrets" / "audio.key"
    state_dir = tmp_path / "working"
    initialize_key(
        key_path,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        state_dir=state_dir,
    )
    original_key = key_path.read_bytes()
    fingerprint = state_dir / ".audio_master_key.sha256"
    assert fingerprint.exists()

    key_path.unlink()
    with pytest.raises(RuntimeError, match="restore the original key"):
        initialize_key(
            key_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            state_dir=state_dir,
        )
    assert not key_path.exists()

    key_path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    with pytest.raises(RuntimeError, match="does not match"):
        initialize_key(
            key_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            state_dir=state_dir,
        )

    key_path.write_bytes(original_key)
    initialize_key(
        key_path,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        state_dir=state_dir,
    )


def test_existing_ciphertext_blocks_first_time_key_generation(tmp_path) -> None:
    key_path = tmp_path / "secrets" / "audio.key"
    state_dir = tmp_path / "working"
    state_dir.mkdir()
    (state_dir / "recording.wav.enc").write_bytes(b"ciphertext")

    with pytest.raises(RuntimeError, match="restore the original key"):
        initialize_key(
            key_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            state_dir=state_dir,
        )

    assert not key_path.exists()
