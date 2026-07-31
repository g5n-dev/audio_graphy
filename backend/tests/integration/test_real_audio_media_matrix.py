"""Real ffmpeg/ffprobe contract tests for canonical reception audio."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from audio_graphy.core.audio_assembler import (
    AudioAssembler,
    AudioAssemblySource,
)


def _write_tone_wav(path: Path, *, duration_sec: float = 3.0) -> None:
    sample_rate = 16_000
    frame_count = round(sample_rate * duration_sec)
    frames = bytearray()
    for sample_index in range(frame_count):
        sample = round(
            8_000
            * math.sin(
                2 * math.pi * 440 * sample_index / sample_rate,
            )
        )
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def _transcode(source: Path, target: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            str(target),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.integration
@pytest.mark.parametrize("source_suffix", [".wav", ".mp3", ".aac"])
async def test_real_media_slice_gap_and_sample_grid_are_isomorphic(
    tmp_path: Path,
    source_suffix: str,
) -> None:
    """Every supported fixture must produce the exact canonical PCM sample count."""

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("ffmpeg and ffprobe are required by the media quality gate")

    root = tmp_path / "media"
    root.mkdir()
    canonical_wav = root / "canonical.wav"
    _write_tone_wav(canonical_wav)
    second_wav = root / "canonical-second.wav"
    _write_tone_wav(second_wav)
    first_source = canonical_wav
    second_source = second_wav
    if source_suffix != ".wav":
        first_source = root / f"source-first{source_suffix}"
        second_source = root / f"source-second{source_suffix}"
        _transcode(canonical_wav, first_source)
        _transcode(second_wav, second_source)

    result = await AudioAssembler(root).assemble(
        [
            AudioAssemblySource(
                path=first_source,
                source_start_sec=0.25,
                source_end_sec=1.25,
            ),
            AudioAssemblySource(
                path=second_source,
                source_start_sec=1.5,
                source_end_sec=2.75,
                gap_before_sec=0.375,
            ),
        ],
        f"assembled-{source_suffix[1:]}.wav",
    )

    output = root / result.output_path
    with wave.open(str(output), "rb") as assembled:
        assert assembled.getframerate() == 16_000
        assert assembled.getnchannels() == 1
        assert assembled.getsampwidth() == 2
        assert assembled.getnframes() == 42_000

    assert result.command_mode == "transcode_pcm"
    assert result.total_duration_sec == pytest.approx(2.625)
    assert [
        (
            item.source_start_sec,
            item.source_end_sec,
            item.gap_before_sec,
            item.timeline_start_sec,
            item.timeline_end_sec,
        )
        for item in result.inputs
    ] == pytest.approx(
        [
            (0.25, 1.25, 0.0, 0.0, 1.0),
            (1.5, 2.75, 0.375, 1.375, 2.625),
        ]
    )
