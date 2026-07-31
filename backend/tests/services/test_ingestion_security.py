"""Security boundaries for API-driven recording ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import APIError, DuplicateRecordingError, ValidationError
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


class _RecordingCrypto:
    def __init__(self) -> None:
        self.calls = 0

    def encrypt_file(self, source: Path, target: Path) -> Any:
        self.calls += 1
        payload = f"ciphertext-generation-{self.calls}".encode()
        target.write_bytes(payload)
        return SimpleNamespace(
            master_key_id="test-master",
            data_key_id=f"key-{self.calls}",
            size_bytes=source.stat().st_size,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )


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


@pytest.mark.asyncio
async def test_duplicate_registration_never_touches_published_ciphertext(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio = tmp_path / "stable.wav"
    audio.write_bytes(b"RIFF-private")
    crypto = _RecordingCrypto()
    service = IngestionService(session_factory, crypto=cast(Any, crypto))

    first = await service.register_recording(
        "tenant-a",
        RecordingCreate(store_id="S1", path=str(audio)),
    )
    assert first.audio_encrypted_path is not None
    published_path = Path(first.audio_encrypted_path)
    published_ciphertext = await asyncio.to_thread(published_path.read_bytes)

    with pytest.raises(DuplicateRecordingError):
        await service.register_recording(
            "tenant-a",
            RecordingCreate(store_id="S1", path=str(audio)),
        )

    assert crypto.calls == 1
    assert await asyncio.to_thread(published_path.read_bytes) == published_ciphertext


def test_recording_response_never_exposes_server_path() -> None:
    assert "path" not in RecordingResponse.model_fields
