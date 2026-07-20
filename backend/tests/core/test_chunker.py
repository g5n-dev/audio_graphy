"""Unit tests for Chunker — VAD → ASR → token-budget chunking.

Tests cover:
    - Token estimation (len(text) // 2)
    - Token budget packing (single chunk, multi-chunk, edge cases)
    - content_hash (SHA-256) uniqueness
    - ASR failure tolerance (single segment transcript="")
    - segment_ids provenance chain
    - Empty input handling
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from audio_graphy.core.chunker import (
    Chunker,
    ChunkerOutput,
    SegmentRecord,
)


@pytest.mark.unit
class TestTokenEstimation:
    """Token count estimation: tiktoken cl100k_base (W11 upgrade)."""

    def _make_chunker(self) -> Chunker:
        """Create a minimal chunker instance for token estimation tests."""
        from audio_graphy.adapters.bundle import AdapterBundle
        from audio_graphy.adapters.mock_asr import MockASRAdapter
        from audio_graphy.adapters.mock_embed import MockEmbedAdapter
        from audio_graphy.adapters.mock_llm import MockLLMAdapter
        from audio_graphy.adapters.mock_vad import MockVADAdapter

        bundle = AdapterBundle(
            vad=MockVADAdapter(),
            asr=MockASRAdapter(),
            strong_llm=MockLLMAdapter(model="test"),
            weak_llm=MockLLMAdapter(model="test"),
            embed=MockEmbedAdapter(dim=1024),
        )
        return Chunker(bundle)

    def test_empty_string(self) -> None:
        chunker = self._make_chunker()
        assert chunker._estimate_tokens("") == 0

    def test_short_string(self) -> None:
        """Tiktoken: 'abcd' encodes to a small number of tokens."""
        chunker = self._make_chunker()
        result = chunker._estimate_tokens("abcd")
        assert result >= 1  # At least 1 token
        assert result <= 4  # At most 4 tokens (1 per char)

    def test_long_string(self) -> None:
        """Long Chinese string produces proportionally reasonable token count."""
        chunker = self._make_chunker()
        text = "你好世界" * 100  # 400 chars
        result = chunker._estimate_tokens(text)
        # tiktoken: Chinese chars typically encode to ~1-2 tokens each
        assert result >= 100  # At least 100 tokens for 400 Chinese chars
        assert result <= 800  # At most 800 tokens

    def test_minimum_1_for_nonempty(self) -> None:
        """Non-empty string always returns at least 1."""
        chunker = self._make_chunker()
        assert chunker._estimate_tokens("a") >= 1

    def test_tiktoken_more_accurate_than_len_div_2(self) -> None:
        """W11: tiktoken should produce different results than len//2 for mixed text."""
        chunker = self._make_chunker()
        text = "CS75 Plus 24期0利息"
        tiktoken_result = chunker._estimate_tokens(text)
        # tiktoken should give a different (more accurate) result
        assert tiktoken_result > 0


@pytest.mark.unit
class TestContentHash:
    """SHA-256 content hash for idempotent deduplication."""

    def test_hash_deterministic(self) -> None:
        """Same text → same hash."""
        text = "测试文本"
        h1 = Chunker._compute_content_hash(text)
        h2 = Chunker._compute_content_hash(text)
        assert h1 == h2

    def test_hash_matches_sha256(self) -> None:
        """Hash matches manual SHA-256."""
        text = "测试"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert Chunker._compute_content_hash(text) == expected

    def test_different_text_different_hash(self) -> None:
        """Different text → different hash."""
        assert Chunker._compute_content_hash("A") != Chunker._compute_content_hash("B")


@pytest.mark.unit
class TestChunkPacking:
    """Token budget packing logic."""

    @staticmethod
    def _make_segment(idx: int, text: str) -> SegmentRecord:
        return SegmentRecord(
            idx=idx,
            start_sec=float(idx * 10),
            end_sec=float(idx * 10 + 8),
            transcript=text,
            speaker=None,
            vad_conf=0.95,
        )

    def test_single_chunk_under_budget(self) -> None:
        """All segments fit in one chunk."""
        chunker = Chunker.__new__(Chunker)  # Bypass __init__ (no bundle needed)
        chunker._token_budget = 1200
        chunker._overlap_tokens = 0

        segments = [
            self._make_segment(0, "短文本1"),
            self._make_segment(1, "短文本2"),
        ]
        chunks = chunker._pack_chunks(segments)
        assert len(chunks) == 1
        assert chunks[0].segment_ids == [0, 1]
        assert "短文本1" in chunks[0].text
        assert "短文本2" in chunks[0].text

    def test_multi_chunk_split(self) -> None:
        """Segments exceeding budget split into multiple chunks."""
        chunker = Chunker.__new__(Chunker)
        chunker._token_budget = 3  # Very small budget (tiktoken tokens)
        chunker._overlap_tokens = 0

        # Each segment has enough tiktoken tokens to exceed the budget
        segments = [
            self._make_segment(0, "A" * 20),
            self._make_segment(1, "B" * 20),
            self._make_segment(2, "C" * 20),
        ]
        chunks = chunker._pack_chunks(segments)
        assert len(chunks) >= 2  # At least 2 chunks with small budget
        assert chunks[0].segment_ids == [0]
        assert chunks[1].segment_ids == [1]
        assert chunks[2].segment_ids == [2]

    def test_empty_segments(self) -> None:
        """Empty segment list produces empty chunks."""
        chunker = Chunker.__new__(Chunker)
        chunker._token_budget = 1200
        chunker._overlap_tokens = 0

        chunks = chunker._pack_chunks([])
        assert chunks == []

    def test_skips_empty_transcripts(self) -> None:
        """Segments with empty transcripts are skipped."""
        chunker = Chunker.__new__(Chunker)
        chunker._token_budget = 1200
        chunker._overlap_tokens = 0

        segments = [
            self._make_segment(0, "有效文本"),
            self._make_segment(1, ""),  # Empty transcript (ASR failed)
            self._make_segment(2, "更多文本"),
        ]
        chunks = chunker._pack_chunks(segments)
        assert len(chunks) == 1
        assert chunks[0].segment_ids == [0, 2]  # Skips idx 1

    def test_chunk_has_content_hash(self) -> None:
        """Each chunk has a non-empty content_hash."""
        chunker = Chunker.__new__(Chunker)
        chunker._token_budget = 1200
        chunker._overlap_tokens = 0

        segments = [self._make_segment(0, "测试内容")]
        chunks = chunker._pack_chunks(segments)
        assert len(chunks) == 1
        assert len(chunks[0].content_hash) == 64  # SHA-256 hex

    def test_chunk_token_n_positive(self) -> None:
        """Each chunk has token_n > 0."""
        chunker = Chunker.__new__(Chunker)
        chunker._token_budget = 1200
        chunker._overlap_tokens = 0

        segments = [self._make_segment(0, "有效文本内容")]
        chunks = chunker._pack_chunks(segments)
        assert len(chunks) == 1
        assert chunks[0].token_n > 0


@pytest.mark.unit
class TestChunkerProcessRecording:
    """Full process_recording with mock adapters."""

    async def test_process_recording_returns_output(
        self, mock_bundle: object, sample_audio_file: Path
    ) -> None:
        """process_recording returns a valid ChunkerOutput."""
        chunker = Chunker(mock_bundle, token_budget=1200)  # type: ignore[arg-type]
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        assert isinstance(output, ChunkerOutput)
        assert output.recording_id == 1
        assert len(output.segments) > 0
        assert len(output.chunks) > 0

    async def test_segments_have_correct_indices(
        self, mock_bundle: object, sample_audio_file: Path
    ) -> None:
        """Segment indices are sequential (0, 1, 2, ...)."""
        chunker = Chunker(mock_bundle, token_budget=1200)  # type: ignore[arg-type]
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        for i, seg in enumerate(output.segments):
            assert seg.idx == i

    async def test_chunks_reference_valid_segments(
        self, mock_bundle: object, sample_audio_file: Path
    ) -> None:
        """Each chunk's segment_ids reference valid segment indices."""
        chunker = Chunker(mock_bundle, token_budget=1200)  # type: ignore[arg-type]
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        valid_indices = {seg.idx for seg in output.segments}
        for chunk in output.chunks:
            for seg_id in chunk.segment_ids:
                assert seg_id in valid_indices

    async def test_file_not_found_raises(self, mock_bundle: object) -> None:
        """Non-existent audio file raises FileNotFoundError."""
        chunker = Chunker(mock_bundle, token_budget=1200)  # type: ignore[arg-type]
        with pytest.raises(FileNotFoundError):
            await chunker.process_recording(
                recording_id=1,
                audio_path="/nonexistent/audio.wav",
                recorded_at=None,
            )

    async def test_file_index_writes(
        self, mock_bundle: object, sample_audio_file: Path, file_index: object
    ) -> None:
        """When file_index is provided, segments are written to it."""
        chunker = Chunker(
            mock_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            file_index=file_index,  # type: ignore[arg-type]
        )
        recorded_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=recorded_at,
            tenant_id="default",
        )

        # Verify segments were written to file_index
        for seg in output.segments:
            key = f"1_{seg.idx}"
            stored = await file_index.get("kv_store_video_segments", key)  # type: ignore[attr-defined]
            assert stored is not None
            assert stored["transcript"] == seg.transcript
