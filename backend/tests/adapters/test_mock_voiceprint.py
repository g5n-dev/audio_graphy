"""Tests for MockVoiceprintAdapter — diarization + voiceprint coherence."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter


@pytest.mark.asyncio
async def test_diarize_returns_speakers(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    result = await adapter.diarize(str(p))
    assert result.num_speakers == 2
    speaker_ids = {s.speaker_id for s in result.segments}
    assert speaker_ids == {"spk_0", "spk_1"}
    assert result.duration_sec > 0


@pytest.mark.asyncio
async def test_diarize_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r1 = await adapter.diarize(str(p))
    r2 = await adapter.diarize(str(p))
    assert r1.segments == r2.segments


@pytest.mark.asyncio
async def test_diarize_max_speakers_honored(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockVoiceprintAdapter(latency_ms=0, num_speakers=4)
    result = await adapter.diarize(str(p), max_speakers=2)
    assert result.num_speakers <= 2


@pytest.mark.asyncio
async def test_voiceprint_l2_normalized(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r = await adapter.extract_voiceprint(str(p), speaker_id="spk_0")
    norm = math.sqrt(sum(v * v for v in r.vector))
    assert norm == pytest.approx(1.0, abs=1e-6)
    assert r.dim == 192


@pytest.mark.asyncio
async def test_voiceprint_same_speaker_high_cosine(tmp_path: Path) -> None:
    """Same speaker_id across different files → cos ≥ 0.6 (design target)."""
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r1 = await adapter.extract_voiceprint(str(p1), speaker_id="agent_zhang")
    r2 = await adapter.extract_voiceprint(str(p2), speaker_id="agent_zhang")
    cos = sum(a * b for a, b in zip(r1.vector, r2.vector, strict=True))
    assert cos >= 0.6, f"expected same-speaker cos ≥ 0.6, got {cos}"


@pytest.mark.asyncio
async def test_voiceprint_different_speakers_low_cosine(tmp_path: Path) -> None:
    """Different speaker_ids → cos ≤ 0.3 (design target)."""
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r1 = await adapter.extract_voiceprint(str(p1), speaker_id="agent_zhang")
    r2 = await adapter.extract_voiceprint(str(p2), speaker_id="customer_li")
    cos = sum(a * b for a, b in zip(r1.vector, r2.vector, strict=True))
    assert cos <= 0.3, f"expected diff-speaker cos ≤ 0.3, got {cos}"


@pytest.mark.asyncio
async def test_voiceprint_no_speaker_id_no_bias(tmp_path: Path) -> None:
    """Empty speaker_id → no bias injection; two files produce dissimilar vectors."""
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r1 = await adapter.extract_voiceprint(str(p1))
    r2 = await adapter.extract_voiceprint(str(p2))
    # No bias → default hash-derived vectors → cos should be near 0 (random L2).
    cos = sum(a * b for a, b in zip(r1.vector, r2.vector, strict=True))
    assert -0.5 < cos < 0.5


@pytest.mark.asyncio
async def test_voiceprint_deterministic_for_path_and_speaker(
    tmp_path: Path,
) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockVoiceprintAdapter(latency_ms=0)
    r1 = await adapter.extract_voiceprint(str(p), speaker_id="agent_zhang")
    r2 = await adapter.extract_voiceprint(str(p), speaker_id="agent_zhang")
    assert r1.vector == r2.vector


def test_invalid_dim() -> None:
    with pytest.raises(ValueError):
        MockVoiceprintAdapter(dim=4)  # < _SPEAKER_BIAS_DIMS


def test_invalid_num_speakers() -> None:
    with pytest.raises(ValueError):
        MockVoiceprintAdapter(num_speakers=0)
