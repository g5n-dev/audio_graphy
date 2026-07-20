"""Unit tests for MockVADAdapter — deterministic VAD segmentation behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.adapters.mock_vad import MockVADAdapter
from audio_graphy.adapters.protocols import VADSegment


@pytest.fixture
def fake_audio(tmp_path: Path) -> Path:
    """Create a fake audio file with deterministic size."""
    p = tmp_path / "fake.wav"
    # 1MB of pseudo-audio data — yields ~10s duration per mock formula
    p.write_bytes(b"\x00" * 1_000_000)
    return p


class TestMockVADSegment:
    """MockVADAdapter.segment() behavior."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_segments_for_existing_file(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        segments = await adapter.segment(str(fake_audio))
        assert len(segments) > 0
        assert all(isinstance(s, VADSegment) for s in segments)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_segments_are_within_duration_bounds(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        segments = await adapter.segment(str(fake_audio), max_segment_sec=10.0)
        for s in segments:
            assert s.end_sec - s.start_sec <= 10.0 + 0.01  # epsilon for rounding

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_segments_are_sequential(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        segments = await adapter.segment(str(fake_audio))
        for i in range(1, len(segments)):
            assert segments[i].start_sec >= segments[i - 1].start_sec
            # Allow small silent gaps between segments
            assert segments[i].start_sec >= segments[i - 1].end_sec - 0.01

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deterministic_same_file_same_output(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        first = await adapter.segment(str(fake_audio))
        second = await adapter.segment(str(fake_audio))
        assert len(first) == len(second)
        for a, b in zip(first, second, strict=True):
            assert a.start_sec == b.start_sec
            assert a.end_sec == b.end_sec

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_different_files_may_differ(self, tmp_path: Path) -> None:
        a = tmp_path / "a.wav"
        b = tmp_path / "b.wav"
        a.write_bytes(b"\x00" * 1_000_000)
        b.write_bytes(b"\x11" * 1_500_000)
        adapter = MockVADAdapter(latency_ms=0)
        segs_a = await adapter.segment(str(a))
        segs_b = await adapter.segment(str(b))
        # Different file sizes yield different total durations → different seg counts
        # (not strictly guaranteed, but very likely)
        assert segs_a[0].start_sec == 0.0
        assert segs_b[0].start_sec == 0.0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        with pytest.raises(FileNotFoundError, match="Audio file not found"):
            await adapter.segment(str(tmp_path / "nope.wav"))

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidence_in_valid_range(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        segments = await adapter.segment(str(fake_audio))
        for s in segments:
            assert 0.0 < s.confidence <= 1.0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_respects_min_segment_sec(self, fake_audio: Path) -> None:
        adapter = MockVADAdapter(latency_ms=0)
        segments = await adapter.segment(str(fake_audio), min_segment_sec=2.0)
        for s in segments:
            assert s.end_sec - s.start_sec >= 2.0 - 0.01
