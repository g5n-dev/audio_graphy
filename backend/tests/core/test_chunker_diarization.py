"""Unit tests for Chunker M7 diarization integration.

Covers:
- ``enable_voiceprint=False`` zero side-effect (M3-M6 regression).
- ``enable_voiceprint=True`` with mock voiceprint adapter — speaker tagging.
- Diarization adapter failure falls back to ``speaker=None`` per file.
- ``_match_speaker`` heuristic: midpoint-then-overlap.

These tests use the Chunker._transcribe_segments() direct entry point
(no DB writes, no MySQL dependency) so they run in milliseconds.
"""

from __future__ import annotations

import pytest

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.mock_asr import MockASRAdapter
from audio_graphy.adapters.mock_embed import MockEmbedAdapter
from audio_graphy.adapters.mock_llm import MockLLMAdapter
from audio_graphy.adapters.mock_vad import MockVADAdapter
from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter
from audio_graphy.adapters.protocols import (
    DiarizationResult,
    DiarizationSegment,
    VADSegment,
)
from audio_graphy.core.chunker import Chunker

# ============================================================
# Bundle fixtures
# ============================================================


def _make_bundle(
    *,
    enable_voiceprint: bool = False,
) -> AdapterBundle:
    """Build a bundle with all mock adapters + optional voiceprint."""
    return AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(flaky=False),
        strong_llm=MockLLMAdapter(model="test"),
        weak_llm=MockLLMAdapter(model="test"),
        embed=MockEmbedAdapter(dim=1024),
        voiceprint=MockVoiceprintAdapter() if enable_voiceprint else None,
    )


class _ScriptedVAD:
    """VAD adapter returning a fixed segment list (skips audio parsing)."""

    def __init__(self, segments: list[VADSegment]) -> None:
        self._segments = segments
        # Attribute expected by AdapterBundle type checker.
        self.model = "scripted-vad"

    async def segment(self, audio_path: str) -> list[VADSegment]:
        return list(self._segments)


class _ScriptedVoiceprint:
    """Voiceprint adapter returning a fixed diarization timeline."""

    def __init__(
        self,
        *,
        segments: list[DiarizationSegment] | None = None,
        raise_on_diarize: Exception | None = None,
    ) -> None:
        self._segments = segments or []
        self._raise = raise_on_diarize
        self.model = "scripted-vp"
        self.diarize_called = 0
        self.extract_called = 0

    async def diarize(self, audio_path: str) -> DiarizationResult:
        self.diarize_called += 1
        if self._raise is not None:
            raise self._raise
        return DiarizationResult(
            segments=tuple(self._segments),
            num_speakers=len({s.speaker_id for s in self._segments}),
            model="scripted-vp",
        )

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "spk_0",
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ):
        self.extract_called += 1
        from audio_graphy.adapters.protocols import VoiceprintResult

        return VoiceprintResult(
            vector=tuple(0.0 for _ in range(192)),
            dim=192,
            model="scripted-vp",
            speaker_id=speaker_id,
            duration_sec=1.0,
        )


def _make_chunker(
    vad_segments: list[VADSegment],
    *,
    enable_voiceprint: bool = False,
    voiceprint_segments: list[DiarizationSegment] | None = None,
    voiceprint_raise: Exception | None = None,
) -> tuple[Chunker, _ScriptedVoiceprint | None]:
    """Construct a Chunker with scripted VAD + optional scripted voiceprint."""
    vp = (
        _ScriptedVoiceprint(segments=voiceprint_segments, raise_on_diarize=voiceprint_raise)
        if enable_voiceprint
        else None
    )
    bundle = AdapterBundle(
        vad=_ScriptedVAD(vad_segments),  # type: ignore[arg-type]
        asr=MockASRAdapter(flaky=False),
        strong_llm=MockLLMAdapter(model="test"),
        weak_llm=MockLLMAdapter(model="test"),
        embed=MockEmbedAdapter(dim=1024),
        voiceprint=vp,  # type: ignore[arg-type]
    )
    chunker = Chunker(
        bundle=bundle,
        session_factory=None,  # type: ignore[arg-type]
        enable_voiceprint=enable_voiceprint,
    )
    return chunker, vp


# ============================================================
# enable_voiceprint=False regression (zero side-effect)
# ============================================================


@pytest.mark.asyncio
class TestChunkerVoiceprintDisabled:
    async def test_no_diarize_call_when_disabled(self) -> None:
        """When enable_voiceprint=False, voiceprint.diarize() is never called."""
        segs = [VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9)]
        chunker, vp = _make_chunker(segs, enable_voiceprint=False)
        assert vp is None  # no voiceprint adapter at all

        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert len(records) == 1
        assert records[0].speaker is None
        assert records[0].idx == 0

    async def test_speaker_none_for_all_segments_when_disabled(self) -> None:
        """Many segments, all should have speaker=None when flag off."""
        segs = [
            VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9),
            VADSegment(start_sec=2.5, end_sec=4.5, confidence=0.85),
            VADSegment(start_sec=5.0, end_sec=7.0, confidence=0.92),
        ]
        chunker, _ = _make_chunker(segs, enable_voiceprint=False)
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert all(r.speaker is None for r in records)
        assert len(records) == 3

    async def test_voiceprint_adapter_in_bundle_but_flag_off(
        self,
    ) -> None:
        """If bundle has voiceprint adapter but flag is off, diarize is skipped."""
        segs = [VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9)]
        # Provide voiceprint adapter but enable_voiceprint=False.
        vp = _ScriptedVoiceprint(
            segments=[DiarizationSegment(speaker_id="spk_0", start_sec=0.0, end_sec=2.0)]
        )
        bundle = AdapterBundle(
            vad=_ScriptedVAD(segs),  # type: ignore[arg-type]
            asr=MockASRAdapter(flaky=False),
            strong_llm=MockLLMAdapter(model="test"),
            weak_llm=MockLLMAdapter(model="test"),
            embed=MockEmbedAdapter(dim=1024),
            voiceprint=vp,  # type: ignore[arg-type]
        )
        chunker = Chunker(
            bundle=bundle,
            session_factory=None,  # type: ignore[arg-type]
            enable_voiceprint=False,
        )

        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert vp.diarize_called == 0
        assert all(r.speaker is None for r in records)


# ============================================================
# enable_voiceprint=True
# ============================================================


@pytest.mark.asyncio
class TestChunkerVoiceprintEnabled:
    async def test_diarize_called_once_per_file(self) -> None:
        segs = [
            VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9),
            VADSegment(start_sec=2.5, end_sec=4.5, confidence=0.85),
            VADSegment(start_sec=5.0, end_sec=7.0, confidence=0.92),
        ]
        diar = [
            DiarizationSegment(speaker_id="spk_0", start_sec=0.0, end_sec=3.5),
            DiarizationSegment(speaker_id="spk_1", start_sec=4.0, end_sec=7.5),
        ]
        chunker, vp = _make_chunker(segs, enable_voiceprint=True, voiceprint_segments=diar)
        await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert vp is not None
        assert vp.diarize_called == 1  # exactly once

    async def test_speaker_assigned_via_midpoint(self) -> None:
        """Midpoint of VAD seg inside diar segment → speaker_id returned."""
        segs = [
            VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9),  # mid=1, in spk_0
            VADSegment(start_sec=4.5, end_sec=6.5, confidence=0.9),  # mid=5.5, in spk_1
        ]
        diar = [
            DiarizationSegment(speaker_id="spk_0", start_sec=0.0, end_sec=3.5),
            DiarizationSegment(speaker_id="spk_1", start_sec=4.0, end_sec=7.5),
        ]
        chunker, _ = _make_chunker(segs, enable_voiceprint=True, voiceprint_segments=diar)
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert records[0].speaker == "spk_0"
        assert records[1].speaker == "spk_1"

    async def test_speaker_assigned_via_overlap_when_no_midpoint_match(
        self,
    ) -> None:
        """VAD seg straddles two diar segs — overlap picks the bigger one."""
        segs = [
            # VAD 3.0–4.5: overlaps spk_0 (3.0–3.5 = 0.5s) and spk_1 (4.0–4.5 = 0.5s).
            # Tie → first one wins (spk_0 in iteration order).
            VADSegment(start_sec=3.0, end_sec=4.5, confidence=0.9),
        ]
        diar = [
            DiarizationSegment(speaker_id="spk_0", start_sec=0.0, end_sec=3.5),
            DiarizationSegment(speaker_id="spk_1", start_sec=4.0, end_sec=7.5),
        ]
        chunker, _ = _make_chunker(segs, enable_voiceprint=True, voiceprint_segments=diar)
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        # midpoint=3.75 not in any diar seg → fallback to max overlap (tie → first).
        assert records[0].speaker in {"spk_0", "spk_1"}

    async def test_no_diar_overlap_returns_none(self) -> None:
        """VAD seg has zero overlap with any diar segment → speaker=None."""
        segs = [
            VADSegment(start_sec=100.0, end_sec=102.0, confidence=0.9),
        ]
        diar = [
            DiarizationSegment(speaker_id="spk_0", start_sec=0.0, end_sec=10.0),
        ]
        chunker, _ = _make_chunker(segs, enable_voiceprint=True, voiceprint_segments=diar)
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert records[0].speaker is None

    async def test_diarize_failure_falls_back_to_speaker_none(
        self,
    ) -> None:
        """When voiceprint.diarize() raises, all speakers default to None."""
        segs = [
            VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9),
            VADSegment(start_sec=2.5, end_sec=4.5, confidence=0.85),
        ]
        chunker, vp = _make_chunker(
            segs,
            enable_voiceprint=True,
            voiceprint_raise=RuntimeError("service down"),
        )
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert vp is not None
        assert vp.diarize_called == 1
        assert all(r.speaker is None for r in records)

    async def test_empty_diar_timeline_returns_none(self) -> None:
        """voiceprint adapter returns 0 speakers → all speaker=None."""
        segs = [VADSegment(start_sec=0.0, end_sec=2.0, confidence=0.9)]
        chunker, _ = _make_chunker(segs, enable_voiceprint=True, voiceprint_segments=[])
        records, _diar = await chunker._transcribe_segments(segs, "/tmp/fake.wav")

        assert records[0].speaker is None


# ============================================================
# _match_speaker heuristic (unit-level)
# ============================================================


class TestMatchSpeaker:
    def test_empty_timeline_returns_none(self) -> None:
        seg = VADSegment(start_sec=0.0, end_sec=1.0, confidence=0.9)
        assert Chunker._match_speaker(seg, []) is None

    def test_midpoint_contained_returns_first_match(self) -> None:
        seg = VADSegment(start_sec=1.0, end_sec=3.0, confidence=0.9)  # mid=2.0
        timeline = [
            DiarizationSegment(speaker_id="spk_A", start_sec=0.0, end_sec=2.5),
            DiarizationSegment(speaker_id="spk_B", start_sec=2.5, end_sec=5.0),
        ]
        assert Chunker._match_speaker(seg, timeline) == "spk_A"

    def test_no_midpoint_uses_overlap(self) -> None:
        seg = VADSegment(start_sec=2.0, end_sec=4.0, confidence=0.9)  # mid=3.0
        # Neither diar seg contains 3.0.
        timeline = [
            DiarizationSegment(speaker_id="spk_A", start_sec=0.0, end_sec=2.5),
            DiarizationSegment(speaker_id="spk_B", start_sec=3.5, end_sec=5.0),
        ]
        # Overlap: A=0.5, B=0.5. Tie → first wins.
        result = Chunker._match_speaker(seg, timeline)
        assert result in {"spk_A", "spk_B"}

    def test_clear_overlap_winner(self) -> None:
        seg = VADSegment(start_sec=2.0, end_sec=4.5, confidence=0.9)  # mid=3.25
        timeline = [
            DiarizationSegment(speaker_id="spk_A", start_sec=0.0, end_sec=2.1),  # 0.1s
            DiarizationSegment(speaker_id="spk_B", start_sec=2.5, end_sec=4.5),  # 2.0s
        ]
        # B has more overlap; A wins midpoint? midpoint 3.25 → not in A; in B (2.5-4.5).
        # So midpoint match returns B.
        assert Chunker._match_speaker(seg, timeline) == "spk_B"

    def test_zero_overlap_returns_none(self) -> None:
        seg = VADSegment(start_sec=100.0, end_sec=200.0, confidence=0.9)
        timeline = [
            DiarizationSegment(speaker_id="spk_A", start_sec=0.0, end_sec=10.0),
            DiarizationSegment(speaker_id="spk_B", start_sec=20.0, end_sec=30.0),
        ]
        assert Chunker._match_speaker(seg, timeline) is None


# ============================================================
# Construction smoke
# ============================================================


class TestChunkerConstruction:
    def test_enable_voiceprint_default_false(self) -> None:
        chunker = Chunker(
            bundle=_make_bundle(),
            session_factory=None,  # type: ignore[arg-type]
        )
        assert chunker._enable_voiceprint is False

    def test_enable_voiceprint_explicit_true(self) -> None:
        chunker = Chunker(
            bundle=_make_bundle(enable_voiceprint=True),
            session_factory=None,  # type: ignore[arg-type]
            enable_voiceprint=True,
        )
        assert chunker._enable_voiceprint is True
