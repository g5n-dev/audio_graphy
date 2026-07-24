"""Typed configuration boundaries for audio resource hardening."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from audio_graphy.config import Settings


def test_audio_resource_defaults_are_bounded(tmp_path) -> None:
    settings = Settings(
        working_dir=tmp_path,
        master_key_path=str(tmp_path / "master.key"),
    )

    assert settings.max_recording_audio_bytes == 512 * 1024 * 1024
    assert settings.audio_crypto_chunk_size_bytes == 4 * 1024 * 1024
    assert settings.max_request_body_bytes == 16 * 1024 * 1024
    assert settings.audio_assembly_max_processes == 2
    assert settings.audio_assembly_ffprobe_timeout_sec == 30.0
    assert settings.audio_assembly_ffmpeg_timeout_sec == 15 * 60.0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_recording_audio_bytes", 0),
        ("audio_crypto_chunk_size_bytes", 512),
        ("audio_crypto_chunk_size_bytes", 16 * 1024 * 1024 + 1),
        ("max_request_body_bytes", 0),
        ("audio_assembly_max_sources", 0),
        ("audio_assembly_max_total_bytes", -1),
        ("audio_assembly_max_estimated_pcm_bytes", 0),
        ("audio_assembly_max_temporary_bytes", 0),
        ("audio_assembly_max_processes", 0),
        ("audio_assembly_ffprobe_timeout_sec", 0),
        ("audio_assembly_ffmpeg_timeout_sec", math.inf),
    ),
)
def test_invalid_audio_resource_configuration_is_rejected(
    tmp_path,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            working_dir=tmp_path,
            master_key_path=str(tmp_path / "master.key"),
            **{field: value},
        )
