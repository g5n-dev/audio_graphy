"""Shared fixtures for real adapter tests.

Key patterns:
- ``tmp_wav`` — generate a tiny valid WAV in tmp_path (no external assets needed).
- ``vad_settings`` — settings override enabling ``adapter_vad_mode="real"``.
- respx is per-test via the ``respx_mock`` auto-fixture from pytest-respx.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from audio_graphy.config import Settings


@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    """Generate a minimal valid WAV (1 second of silence, 16 kHz mono)."""
    wav_path = tmp_path / "test.wav"
    sample_rate = 16000
    duration_sec = 1
    n_samples = sample_rate * duration_sec
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + "h" * n_samples, *([0] * n_samples)))
    return wav_path


@pytest.fixture
def vad_settings() -> Settings:
    """Settings with ``adapter_vad_mode="real"`` and a *.test URL (respx intercepts)."""
    return Settings(
        adapter_vad_mode="real",
        silero_vad_url="http://silero-vad.test",
    )
