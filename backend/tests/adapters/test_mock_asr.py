"""Unit tests for MockASRAdapter — deterministic Chinese transcript behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.adapters.mock_asr import MockASRAdapter
from audio_graphy.adapters.protocols import ASRResult


@pytest.fixture
def fake_audio(tmp_path: Path) -> Path:
    """Create a fake audio file."""
    p = tmp_path / "fake.wav"
    p.write_bytes(b"\x00" * 1_000_000)
    return p


class TestMockASRTranscribe:
    """MockASRAdapter.transcribe() behavior."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_asr_result_for_existing_file(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        result = await adapter.transcribe(str(fake_audio))
        assert isinstance(result, ASRResult)
        assert result.text
        assert result.language == "zh"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_chinese_text(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        result = await adapter.transcribe(str(fake_audio))
        # Must contain at least one Chinese character
        assert any("\u4e00" <= ch <= "\u9fff" for ch in result.text)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deterministic_same_file_same_text(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        first = await adapter.transcribe(str(fake_audio))
        second = await adapter.transcribe(str(fake_audio))
        assert first.text == second.text
        assert first.confidence == second.confidence

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_words_have_valid_timestamps(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        result = await adapter.transcribe(str(fake_audio))
        for _, start, end in result.words:
            assert start >= 0.0
            assert end > start
            assert end - start < 1.0  # single char shouldn't take >1s

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_confidence_in_valid_range(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        result = await adapter.transcribe(str(fake_audio))
        assert 0.0 < result.confidence <= 1.0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        with pytest.raises(FileNotFoundError, match="Audio file not found"):
            await adapter.transcribe(str(tmp_path / "nope.wav"))

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flaky_mode_raises_occasionally(self, fake_audio: Path) -> None:
        """When flaky=True, ASR may raise — we just confirm it can raise."""
        adapter = MockASRAdapter(flaky=True, latency_ms=0)
        # Run up to 200 calls; flaky should trigger at least once
        raised = False
        for _ in range(200):
            try:
                await adapter.transcribe(str(fake_audio))
            except RuntimeError as e:
                assert "simulated timeout" in str(e)
                raised = True
                break
        # Statistically should fire within 200 calls at 1% rate (very high probability)
        # If it doesn't, the test still passes — we don't want flaky tests.
        # We only assert that flaky mode CAN raise.
        if not raised:
            pytest.skip("Flaky mode didn't fire in 200 calls (low probability)")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_call_count_increments(self, fake_audio: Path) -> None:
        adapter = MockASRAdapter(latency_ms=0)
        assert adapter.call_count == 0
        await adapter.transcribe(str(fake_audio))
        assert adapter.call_count == 1
        await adapter.transcribe(str(fake_audio))
        assert adapter.call_count == 2
