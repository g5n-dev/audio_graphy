"""Unit contracts for reception audio assembly boundaries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.errors import APIError
from audio_graphy.services.receptions import (
    ReceptionService,
    _clip_evidence_refs,
    _tag_applies_to_window,
    resolve_safe_audio_output,
)


def test_safe_audio_output_is_resolved_below_reception_root(tmp_path: Path) -> None:
    resolved = resolve_safe_audio_output(
        tmp_path,
        "tenant-a/receptions/reception-42.wav",
    )

    assert resolved == (tmp_path / "tenant-a" / "receptions" / "reception-42.wav").resolve()


@pytest.mark.parametrize(
    "candidate",
    [
        "../escape.wav",
        "/tmp/absolute.wav",
        "tenant-a/../../escape.wav",
        "tenant-a/reception.mp3",
        "tenant-a/.hidden.wav",
    ],
)
def test_safe_audio_output_rejects_traversal_and_non_wav_paths(
    tmp_path: Path,
    candidate: str,
) -> None:
    with pytest.raises(ValueError, match="audio output"):
        resolve_safe_audio_output(tmp_path, candidate)


@pytest.mark.asyncio
async def test_encrypted_audio_is_verified_and_unlinked_after_open(
    tmp_path: Path,
) -> None:
    from audio_graphy.api.receptions import _open_audio_descriptor

    root = tmp_path / "working"
    root.mkdir()
    plaintext = root / "source.wav"
    ciphertext = root / "source.wav.enc"
    plaintext.write_bytes(b"RIFF-private-audio")
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    crypto.encrypt_file(plaintext, ciphertext)
    plaintext.unlink()
    service = ReceptionService(
        cast(Any, None),
        audio_root=root,
        audio_crypto=crypto,
    )

    asset = await service._decrypt_audio_asset(
        str(ciphertext),
        original_path=str(plaintext),
        tenant_id="tenant-a",
    )
    assert asset.path.read_bytes() == b"RIFF-private-audio"
    assert asset.delete_after_open is True

    descriptor, size = _open_audio_descriptor(asset)
    try:
        assert not asset.path.exists()
        assert os.read(descriptor, size) == b"RIFF-private-audio"
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
async def test_tampered_encrypted_audio_never_leaves_plaintext(
    tmp_path: Path,
) -> None:
    root = tmp_path / "working"
    root.mkdir()
    plaintext = root / "source.wav"
    ciphertext = root / "source.wav.enc"
    plaintext.write_bytes(b"RIFF-private-audio")
    crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
    crypto.encrypt_file(plaintext, ciphertext)
    plaintext.unlink()
    ciphertext.write_bytes(ciphertext.read_bytes()[:-8] + b"tampered")
    service = ReceptionService(
        cast(Any, None),
        audio_root=root,
        audio_crypto=crypto,
    )

    with pytest.raises(APIError, match="could not be verified"):
        await service._decrypt_audio_asset(
            str(ciphertext),
            original_path=str(plaintext),
            tenant_id="tenant-a",
        )

    runtime_dir = root / "runtime_plaintext"
    assert not runtime_dir.exists() or not any(runtime_dir.rglob("audio-*"))


def test_tag_window_uses_reception_timeline_not_source_coordinates() -> None:
    tag = cast(
        Any,
        type(
            "_Tag",
            (),
            {
                "evidence_refs": [
                    {
                        "recording_id": 3,
                        "source_start_sec": 101.0,
                        "source_end_sec": 104.0,
                        "timeline_start_sec": 1.0,
                        "timeline_end_sec": 4.0,
                        "coordinate_space": "both",
                    }
                ]
            },
        )(),
    )

    assert _tag_applies_to_window(tag, start_sec=0.0, end_sec=5.0)
    assert not _tag_applies_to_window(tag, start_sec=5.0, end_sec=10.0)


def test_source_only_evidence_is_conservatively_retained() -> None:
    tag = cast(
        Any,
        type(
            "_Tag",
            (),
            {
                "evidence_refs": [
                    {
                        "recording_id": 3,
                        "start_sec": 101.0,
                        "end_sec": 104.0,
                        "coordinate_space": "source",
                    }
                ]
            },
        )(),
    )

    assert _tag_applies_to_window(tag, start_sec=0.0, end_sec=5.0)
    assert _tag_applies_to_window(tag, start_sec=5.0, end_sec=10.0)


def test_clip_evidence_keeps_source_and_timeline_geometry_aligned() -> None:
    clipped = _clip_evidence_refs(
        [
            {
                "recording_id": 3,
                "source_start_sec": 100.0,
                "source_end_sec": 120.0,
                "timeline_start_sec": 10.0,
                "timeline_end_sec": 30.0,
                "start_ms": 10_000,
                "end_ms": 30_000,
                "coordinate_space": "both",
            }
        ],
        start_sec=18.0,
        end_sec=24.0,
    )

    assert len(clipped) == 1
    assert clipped[0]["timeline_start_sec"] == 18.0
    assert clipped[0]["timeline_end_sec"] == 24.0
    assert clipped[0]["source_start_sec"] == 108.0
    assert clipped[0]["source_end_sec"] == 114.0
    assert clipped[0]["start_ms"] == 18_000
    assert clipped[0]["end_ms"] == 24_000
    assert clipped[0]["coordinate_space"] == "both"


def test_clip_evidence_drops_non_overlaps_but_retains_source_only_refs() -> None:
    source_only = {
        "recording_id": 3,
        "start_sec": 101.0,
        "end_sec": 104.0,
        "coordinate_space": "source",
    }
    clipped = _clip_evidence_refs(
        [
            {
                "recording_id": 3,
                "start_sec": 1.0,
                "end_sec": 4.0,
            },
            source_only,
            {"recording_id": 3, "segment_id": 9},
        ],
        start_sec=5.0,
        end_sec=10.0,
    )

    assert clipped == [source_only, {"recording_id": 3, "segment_id": 9}]
