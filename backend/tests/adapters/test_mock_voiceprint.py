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


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestSpeakerIdentityFallback:
    """How the mock guesses identity when scoring cannot supply one.

    Speaker-verification scoring never passes ``speaker_id`` — identity is
    what it measures — so without a fallback the mock makes every clip its
    own speaker and the EER is pure noise.
    """

    async def test_off_by_default_every_file_is_its_own_speaker(self) -> None:
        adapter = MockVoiceprintAdapter(latency_ms=0)
        a = await adapter.extract_voiceprint("/data/id00042/one.flac")
        b = await adapter.extract_voiceprint("/data/id00042/two.flac")
        assert _cos(a.vector, b.vector) < 0.5

    async def test_dirname_mode_groups_by_parent_directory(self) -> None:
        """CN-Celeb's data/ tree: the directory is the speaker."""
        adapter = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="dirname")
        a = await adapter.extract_voiceprint("/data/id00042/interview-01.flac")
        b = await adapter.extract_voiceprint("/data/id00042/interview-02.flac")
        other = await adapter.extract_voiceprint("/data/id00099/interview-01.flac")
        assert _cos(a.vector, b.vector) >= 0.6
        assert _cos(a.vector, other.vector) <= 0.3

    async def test_filename_mode_groups_by_stem_prefix(self) -> None:
        """A flat directory whose files are named by speaker."""
        adapter = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="filename")
        a = await adapter.extract_voiceprint("/clips/alice_01.wav")
        b = await adapter.extract_voiceprint("/clips/alice_02.wav")
        other = await adapter.extract_voiceprint("/clips/bob_01.wav")
        assert _cos(a.vector, b.vector) >= 0.6
        assert _cos(a.vector, other.vector) <= 0.3

    async def test_the_two_modes_disagree_on_the_same_corpus(self) -> None:
        """Which part of the path holds the identity cannot be guessed.

        Under one convention a filename prefix is a recording type shared by
        everyone; under the other the parent directory is. Picking wrong
        collapses the corpus into a single identity, which is why the caller
        must say.
        """
        by_dir = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="dirname")
        by_name = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="filename")
        left = "/data/id00042/interview-01.flac"
        right = "/data/id00099/interview-01.flac"

        dir_cos = _cos(
            (await by_dir.extract_voiceprint(left)).vector,
            (await by_dir.extract_voiceprint(right)).vector,
        )
        name_cos = _cos(
            (await by_name.extract_voiceprint(left)).vector,
            (await by_name.extract_voiceprint(right)).vector,
        )
        # Two different speakers: correct by directory, collapsed by filename.
        assert dir_cos <= 0.3
        assert name_cos >= 0.6
