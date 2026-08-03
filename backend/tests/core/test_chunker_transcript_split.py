"""Chunker splits one transcription across the VAD segments.

Chunker used to call ``transcribe()`` once per VAD segment and store the
returned text on that segment. No adapter honours the ``segments`` argument —
funASR runs its own VAD, and the mock keys off the audio path — so every
segment received the identical whole-file transcript, and an N-segment
recording ran N whole-file inferences to produce it. Concatenating the
segments then yielded the transcript repeated N times, which is what reached
embeddings, chunk packing and the LLM prompts.

Nothing caught it: the suite asserted speaker assignment and PII scrubbing,
never that a segment's text was *its own*. These tests assert exactly that.
"""

from __future__ import annotations

import pytest

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.mock_asr import MockASRAdapter
from audio_graphy.adapters.mock_embed import MockEmbedAdapter
from audio_graphy.adapters.mock_llm import MockLLMAdapter
from audio_graphy.adapters.protocols import ASRResult, VADSegment
from audio_graphy.core.chunker import Chunker


class _ScriptedASR:
    """ASR adapter returning a fixed ASRResult, counting calls."""

    def __init__(
        self,
        result: ASRResult | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self.model = "scripted-asr"
        self.calls = 0
        self.segments_seen: list[list[VADSegment] | None] = []

    async def transcribe(
        self,
        audio_path: str,
        *,
        segments: list[VADSegment] | None = None,
        language: str = "zh",
    ) -> ASRResult:
        self.calls += 1
        self.segments_seen.append(segments)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _chunker(asr: _ScriptedASR) -> Chunker:
    bundle = AdapterBundle(
        vad=None,  # type: ignore[arg-type]
        asr=asr,  # type: ignore[arg-type]
        strong_llm=MockLLMAdapter(model="test"),
        weak_llm=MockLLMAdapter(model="test"),
        embed=MockEmbedAdapter(dim=1024),
        voiceprint=None,
    )
    return Chunker(
        bundle=bundle,
        session_factory=None,  # type: ignore[arg-type]
        enable_voiceprint=False,
    )


# Three well-separated segments; one sentence squarely inside each.
_SEGS = [
    VADSegment(start_sec=0.0, end_sec=3.0, confidence=0.9),
    VADSegment(start_sec=5.0, end_sec=8.0, confidence=0.9),
    VADSegment(start_sec=10.0, end_sec=13.0, confidence=0.9),
]
_RESULT = ASRResult(
    text="第一句。第二句。第三句。",
    language="zh",
    confidence=0.95,
    words=(
        ("第一句。", 0.5, 2.5),
        ("第二句。", 5.5, 7.5),
        ("第三句。", 10.5, 12.5),
    ),
)


@pytest.mark.asyncio
class TestTranscriptSplit:
    async def test_transcribes_once_not_once_per_segment(self) -> None:
        asr = _ScriptedASR(_RESULT)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert asr.calls == 1, "one whole-file pass, not one inference per segment"
        assert len(records) == 3

    async def test_each_segment_gets_its_own_text(self) -> None:
        """The regression: every segment used to receive the whole transcript."""
        asr = _ScriptedASR(_RESULT)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert [r.transcript for r in records] == ["第一句。", "第二句。", "第三句。"]
        assert records[0].transcript != _RESULT.text

    async def test_concatenation_reproduces_the_transcript(self) -> None:
        """No entry duplicated across segments, none dropped."""
        asr = _ScriptedASR(_RESULT)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert "".join(r.transcript for r in records) == _RESULT.text

    async def test_sentence_straddling_a_boundary_lands_in_exactly_one(self) -> None:
        """Filtering per segment would count it twice and inflate the text."""
        result = ASRResult(
            text="跨越边界的一句话。",
            language="zh",
            confidence=0.95,
            # Spans the gap: starts inside segment 0, ends inside segment 1,
            # midpoint (4.0) falls in neither.
            words=(("跨越边界的一句话。", 2.0, 6.0),),
        )
        asr = _ScriptedASR(result)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        placed = [r.transcript for r in records if r.transcript]
        assert placed == ["跨越边界的一句话。"]
        assert "".join(r.transcript for r in records) == result.text

    async def test_entry_inside_a_vad_gap_is_placed_not_dropped(self) -> None:
        """Speech our VAD missed is still real speech; losing it loses text."""
        result = ASRResult(
            text="缝隙里的话。",
            language="zh",
            confidence=0.95,
            words=(("缝隙里的话。", 8.4, 8.6),),  # wholly inside the 8.0-10.0 gap
        )
        asr = _ScriptedASR(result)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert "".join(r.transcript for r in records) == result.text

    async def test_text_without_timings_is_kept_not_silently_lost(self) -> None:
        result = ASRResult(text="没有时间戳的文本。", language="zh", confidence=0.9, words=())
        asr = _ScriptedASR(result)
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert "".join(r.transcript for r in records) == result.text

    async def test_silence_stays_empty_everywhere(self) -> None:
        asr = _ScriptedASR(ASRResult(text="", language="zh", confidence=0.0, words=()))
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert all(r.transcript == "" for r in records)

    async def test_asr_failure_empties_every_transcript_without_raising(self) -> None:
        asr = _ScriptedASR(raises=RuntimeError("ASR down"))
        records, _ = await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert len(records) == 3
        assert all(r.transcript == "" for r in records)

    async def test_full_vad_list_is_offered_to_the_adapter(self) -> None:
        """funASR ignores it, but the mock uses it to place its timestamps."""
        asr = _ScriptedASR(_RESULT)
        await _chunker(asr)._transcribe_segments(_SEGS, "/tmp/x.wav")

        assert asr.segments_seen == [list(_SEGS)]


class TestMockASRCharacterLayout:
    """The mock must spread its synthetic characters over the real segments.

    Anchored at t=0 they would all land in whichever segments sit near the
    start of the file, leaving later segments blank in every mock-mode run —
    a split that looks broken when it is the fixture that is wrong.
    """

    def test_characters_land_inside_the_given_segments(self) -> None:
        words = MockASRAdapter._lay_out_chars("一二三四五六", list(_SEGS))
        assert words
        for _ch, start, end in words:
            assert any(
                seg.start_sec <= start and end <= seg.end_sec + 1e-6 for seg in _SEGS
            ), f"({start}, {end}) fell outside every VAD segment"

    def test_every_character_is_kept(self) -> None:
        text = "一二三四五六七八九十"
        words = MockASRAdapter._lay_out_chars(text, list(_SEGS))
        assert "".join(w[0] for w in words) == text

    def test_reaches_the_last_segment(self) -> None:
        words = MockASRAdapter._lay_out_chars("一二三四五六七八九十", list(_SEGS))
        last = _SEGS[-1]
        assert any(start >= last.start_sec for _ch, start, _end in words)

    def test_without_segments_it_still_starts_at_zero(self) -> None:
        words = MockASRAdapter._lay_out_chars("一二三", None)
        assert words[0][1] == 0.0

    def test_zero_length_segments_fall_back_instead_of_dividing_by_zero(self) -> None:
        degenerate = [VADSegment(start_sec=1.0, end_sec=1.0, confidence=0.9)]
        words = MockASRAdapter._lay_out_chars("一二三", degenerate)
        assert "".join(w[0] for w in words) == "一二三"
