"""Security boundaries for API-driven recording ingestion."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from audio_graphy.errors import APIError, ValidationError
from audio_graphy.schemas.recordings import RecordingCreate, RecordingResponse
from audio_graphy.services.ingestion import IngestionService


class _BrokenCrypto:
    def encrypt_file(self, _source: Path, target: Path) -> None:
        target.write_bytes(b"partial-ciphertext")
        raise RuntimeError("key service unavailable")


class _ThreadCheckingBrokenCrypto:
    def __init__(self) -> None:
        self.called_thread_id: int | None = None

    def encrypt_file(self, _source: Path, _target: Path) -> None:
        self.called_thread_id = threading.get_ident()
        raise RuntimeError("stop after thread check")


def _service(
    root: Path,
    *,
    crypto: object | None = None,
    max_audio_bytes: int = 512 * 1024 * 1024,
) -> IngestionService:
    return IngestionService(
        cast(Any, None),
        crypto=cast(Any, crypto),
        allowed_root=root,
        max_audio_bytes=max_audio_bytes,
    )


def test_managed_audio_path_rejects_escape_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "working"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF")

    with pytest.raises(ValidationError):
        _service(root)._validate_managed_audio_path(str(outside))

    symlink = root / "linked.wav"
    symlink.symlink_to(outside)
    with pytest.raises(ValidationError):
        _service(root)._validate_managed_audio_path(str(symlink))

    managed = root / "managed.wav"
    managed.write_bytes(b"RIFF")
    hardlink = root / "hardlink.wav"
    os.link(managed, hardlink)
    with pytest.raises(ValidationError, match="supported regular file"):
        _service(root)._validate_managed_audio_path(str(managed))


def test_managed_audio_path_accepts_relative_supported_file(tmp_path: Path) -> None:
    root = tmp_path / "working"
    root.mkdir()
    audio = root / "source.wav"
    audio.write_bytes(b"RIFF")

    assert _service(root)._validate_managed_audio_path("source.wav") == audio


def test_managed_audio_path_rejects_file_over_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "working"
    root.mkdir()
    audio = root / "source.wav"
    audio.write_bytes(b"x" * 1025)

    with pytest.raises(ValidationError, match="size limit"):
        _service(root, max_audio_bytes=1024)._validate_managed_audio_path("source.wav")


@pytest.mark.asyncio
async def test_configured_encryption_failure_never_registers_plaintext_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "working"
    root.mkdir()
    audio = root / "source.wav"
    audio.write_bytes(b"RIFF-private")
    service = _service(root, crypto=_BrokenCrypto())

    with pytest.raises(APIError, match="encryption failed"):
        await service.register_recording(
            "tenant-a",
            RecordingCreate(store_id="S1", path=str(audio)),
        )

    assert await asyncio.to_thread(audio.exists)
    assert not await asyncio.to_thread(Path(f"{audio}.enc").exists)


@pytest.mark.asyncio
async def test_encryption_is_offloaded_from_the_event_loop(tmp_path: Path) -> None:
    root = tmp_path / "working"
    root.mkdir()
    audio = root / "source.wav"
    audio.write_bytes(b"RIFF-private")
    crypto = _ThreadCheckingBrokenCrypto()
    event_loop_thread_id = threading.get_ident()
    service = _service(root, crypto=crypto)

    with pytest.raises(APIError, match="encryption failed"):
        await service.register_recording(
            "tenant-a",
            RecordingCreate(store_id="S1", path=str(audio)),
        )

    assert crypto.called_thread_id is not None
    assert crypto.called_thread_id != event_loop_thread_id


def test_recording_response_never_exposes_server_path() -> None:
    assert "path" not in RecordingResponse.model_fields
