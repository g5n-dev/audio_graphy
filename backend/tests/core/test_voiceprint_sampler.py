"""VoiceprintSampler tests — candidate assembly + quality gates (ADR-0001).

Covers the behaviour the linker depends on:
    - weighted_mean averages per-segment embeddings by duration
    - longest_segment extracts exactly once, from the longest segment
    - segment / total-speech gates keep unreliable speakers out
    - the extraction cap keeps the longest segments
    - outlier rejection drops a mis-attributed segment
    - unlabelled segments and adapter failures degrade safely
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from audio_graphy.core.voiceprint_sampler import VoiceprintSampler

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class _Seg:
    """Mirror of chunker.SegmentRecord's sampler-visible surface."""

    start_sec: float
    end_sec: float
    speaker: str | None


@dataclass(frozen=True, slots=True)
class _Result:
    vector: tuple[float, ...]
    dim: int = 3
    model: str = "test"
    speaker_id: str = ""
    duration_sec: float = 0.0


class _FakeVoiceprint:
    """Returns a caller-supplied vector per (start, end) window."""

    def __init__(
        self,
        vectors: dict[tuple[float, float], tuple[float, ...]] | None = None,
        *,
        default: tuple[float, ...] = (1.0, 0.0, 0.0),
        fail: bool = False,
        fail_windows: set[tuple[float, float]] | None = None,
    ) -> None:
        self._vectors = vectors or {}
        self._default = default
        self._fail = fail
        self._fail_windows = fail_windows or set()
        self.calls: list[tuple[float | None, float | None]] = []

    async def diarize(self, audio_path: str, **kwargs: object) -> object:
        raise AssertionError("sampler must not call diarize")

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> _Result:
        self.calls.append((start_sec, end_sec))
        key = (float(start_sec or 0.0), float(end_sec or 0.0))
        if self._fail or key in self._fail_windows:
            raise RuntimeError("campplus unavailable")
        return _Result(vector=self._vectors.get(key, self._default))


def _unit(vec: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(x * x for x in vec))
    return tuple(x / norm for x in vec)


class TestSamplingStrategies:
    async def test_weighted_mean_averages_by_duration(self) -> None:
        """A 9s segment must dominate a 3s one in the centroid."""
        adapter = _FakeVoiceprint(
            {
                (0.0, 9.0): (1.0, 0.0, 0.0),
                (10.0, 13.0): (0.0, 1.0, 0.0),
            }
        )
        report = await VoiceprintSampler(adapter, outlier_cosine=0.0).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 9.0, "spk_0"), _Seg(10.0, 13.0, "spk_0")],
        )
        assert len(report.candidates) == 1
        vec = report.candidates[0].voiceprint
        expected = _unit((9.0, 3.0, 0.0))
        assert vec == pytest.approx(expected)
        # Unit norm is a hard requirement: cosine and the id hash assume it.
        assert math.sqrt(sum(x * x for x in vec)) == pytest.approx(1.0)
        assert report.extract_calls == 2

    async def test_longest_segment_extracts_once_from_longest(self) -> None:
        adapter = _FakeVoiceprint(
            {
                (0.0, 4.0): (0.0, 1.0, 0.0),
                (10.0, 20.0): (1.0, 0.0, 0.0),
            }
        )
        report = await VoiceprintSampler(adapter, strategy="longest_segment").sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 4.0, "spk_0"), _Seg(10.0, 20.0, "spk_0")],
        )
        assert adapter.calls == [(10.0, 20.0)]
        assert report.candidates[0].voiceprint == pytest.approx((1.0, 0.0, 0.0))
        assert report.extract_calls == 1

    def test_unknown_strategy_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown voiceprint sampling strategy"):
            VoiceprintSampler(_FakeVoiceprint(), strategy="mean_of_everything")


class TestQualityGates:
    async def test_short_segments_do_not_count_toward_total(self) -> None:
        """8 x 0.5s is 4s of speech but no usable segment — no candidate."""
        adapter = _FakeVoiceprint()
        segments = [_Seg(i * 2.0, i * 2.0 + 0.5, "spk_0") for i in range(8)]
        report = await VoiceprintSampler(
            adapter,
            min_segment_sec=1.0,
            min_total_sec=3.0,
        ).sample(recording_id=1, audio_path="/tmp/a.wav", segments=segments)

        assert report.candidates == ()
        assert "spk_0" in report.skipped_speakers
        assert "insufficient usable speech" in report.skipped_speakers["spk_0"]
        assert adapter.calls == []  # never pay for an extraction we will discard

    async def test_speaker_below_total_speech_gate_is_skipped(self) -> None:
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(
            adapter,
            min_segment_sec=1.0,
            min_total_sec=5.0,
        ).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 2.0, "spk_0")],
        )
        assert report.candidates == ()
        assert "spk_0" in report.skipped_speakers

    async def test_qualifying_speaker_survives_while_weak_one_is_skipped(self) -> None:
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(
            adapter,
            min_segment_sec=1.0,
            min_total_sec=3.0,
        ).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 10.0, "spk_0"), _Seg(11.0, 12.0, "spk_1")],
        )
        assert [c.speaker_id for c in report.candidates] == ["spk_0"]
        assert list(report.skipped_speakers) == ["spk_1"]

    async def test_extraction_cap_keeps_the_longest_segments(self) -> None:
        adapter = _FakeVoiceprint()
        segments = [_Seg(float(i * 20), float(i * 20 + i + 2), "spk_0") for i in range(6)]
        report = await VoiceprintSampler(adapter, max_segments=2).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=segments,
        )
        assert report.extract_calls == 2
        # The two longest windows are i=5 (7s) and i=4 (6s).
        assert adapter.calls == [(100.0, 107.0), (80.0, 86.0)]

    async def test_speech_sec_reports_all_speech_not_just_sampled(self) -> None:
        """total_speech_sec describes the speaker, not our sampling budget."""
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(adapter, max_segments=1).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 10.0, "spk_0"),
                _Seg(20.0, 25.0, "spk_0"),
                _Seg(30.0, 30.4, "spk_0"),  # below the segment floor
            ],
        )
        assert report.candidates[0].speech_sec == pytest.approx(15.4)

    async def test_sampled_sec_counts_only_audio_behind_the_vector(self) -> None:
        """The template ranking uses this, so it must not inherit speech_sec.

        A speaker with a huge pile of short interjections would otherwise
        outrank a long monologue while resting on a couple of seconds.
        """
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(
            adapter,
            min_segment_sec=1.0,
            min_total_sec=3.0,
            max_segments=2,
        ).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 4.0, "spk_0"),
                _Seg(10.0, 13.0, "spk_0"),
                _Seg(20.0, 25.0, "spk_0"),
                # 60s of sub-second interjections: real speech, unusable audio.
                *[_Seg(100.0 + i, 100.5 + i, "spk_0") for i in range(120)],
            ],
        )
        candidate = report.candidates[0]
        # Two longest usable windows: 5s + 4s.
        assert candidate.sampled_sec == pytest.approx(9.0)
        assert candidate.speech_sec > 70.0
        assert candidate.sampled_sec < candidate.speech_sec

    async def test_outlier_rejection_shrinks_sampled_sec(self) -> None:
        adapter = _FakeVoiceprint(
            {
                (0.0, 5.0): (1.0, 0.0, 0.0),
                (10.0, 15.0): (1.0, 0.05, 0.0),
                (20.0, 30.0): (0.0, 0.0, 1.0),  # someone else, and the longest
            }
        )
        report = await VoiceprintSampler(adapter, outlier_cosine=0.5).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 5.0, "spk_0"),
                _Seg(10.0, 15.0, "spk_0"),
                _Seg(20.0, 30.0, "spk_0"),
            ],
        )
        assert report.dropped_outlier_segments == 1
        # The discarded 10s window must not be credited to the vector.
        assert report.candidates[0].sampled_sec == pytest.approx(10.0)


class TestOutlierRejection:
    async def test_mis_attributed_segment_is_dropped(self) -> None:
        """One diarization error must not drag the centroid off the speaker."""
        adapter = _FakeVoiceprint(
            {
                (0.0, 5.0): (1.0, 0.0, 0.0),
                (10.0, 15.0): (1.0, 0.05, 0.0),
                (20.0, 25.0): (0.0, 0.0, 1.0),  # someone else
            }
        )
        report = await VoiceprintSampler(adapter, outlier_cosine=0.5).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 5.0, "spk_0"),
                _Seg(10.0, 15.0, "spk_0"),
                _Seg(20.0, 25.0, "spk_0"),
            ],
        )
        assert report.dropped_outlier_segments == 1
        # Third component came only from the intruder and is now gone.
        assert report.candidates[0].voiceprint[2] == pytest.approx(0.0)

    async def test_longest_segment_cannot_escape_outlier_rejection(self) -> None:
        """Membership is one-segment-one-vote, not one-second-one-vote.

        Judging against the duration-weighted centroid would let the longest
        segment dominate the very average it is tested against, so a
        mis-attributed long segment would clear its own bar.
        """
        adapter = _FakeVoiceprint(
            {
                (0.0, 5.0): (1.0, 0.0, 0.0),
                (10.0, 15.0): (1.0, 0.05, 0.0),
                (20.0, 60.0): (0.0, 0.0, 1.0),  # intruder, and by far the longest
            }
        )
        report = await VoiceprintSampler(adapter, outlier_cosine=0.5).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 5.0, "spk_0"),
                _Seg(10.0, 15.0, "spk_0"),
                _Seg(20.0, 60.0, "spk_0"),
            ],
        )
        assert report.dropped_outlier_segments == 1
        assert report.candidates[0].voiceprint[2] == pytest.approx(0.0)
        assert report.candidates[0].sampled_sec == pytest.approx(10.0)

    async def test_two_samples_never_trigger_outlier_rejection(self) -> None:
        """With two samples there is no majority — dropping one halves evidence."""
        adapter = _FakeVoiceprint(
            {
                (0.0, 5.0): (1.0, 0.0, 0.0),
                (10.0, 15.0): (0.0, 0.0, 1.0),
            }
        )
        report = await VoiceprintSampler(adapter, outlier_cosine=0.9).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 5.0, "spk_0"), _Seg(10.0, 15.0, "spk_0")],
        )
        assert report.dropped_outlier_segments == 0
        assert len(report.candidates) == 1


class TestDegradedInputs:
    async def test_unlabelled_segments_are_ignored(self) -> None:
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(adapter).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 30.0, None), _Seg(31.0, 32.0, "")],
        )
        assert report.candidates == ()
        assert report.skipped_speakers == {}
        assert adapter.calls == []

    async def test_adapter_failure_skips_speaker_without_raising(self) -> None:
        """A voiceprint outage must not fail the whole indexing run."""
        adapter = _FakeVoiceprint(fail=True)
        report = await VoiceprintSampler(adapter).sample(
            recording_id=7,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 10.0, "spk_0")],
        )
        assert report.candidates == ()
        assert "extraction failed" in report.skipped_speakers["spk_0"]

    async def test_one_failed_window_does_not_discard_the_others(self) -> None:
        """A single bad crop must not throw away the successful embeddings."""
        adapter = _FakeVoiceprint(fail_windows={(20.0, 25.0)})
        report = await VoiceprintSampler(adapter).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 5.0, "spk_0"),
                _Seg(10.0, 15.0, "spk_0"),
                _Seg(20.0, 25.0, "spk_0"),
            ],
        )
        assert len(report.candidates) == 1
        assert report.extract_calls == 3
        # Only the two windows that succeeded back the vector.
        assert report.candidates[0].sampled_sec == pytest.approx(10.0)

    async def test_speaker_fails_when_survivors_drop_below_the_gate(self) -> None:
        """Partial failure must still be judged against the real gate."""
        adapter = _FakeVoiceprint(fail_windows={(10.0, 20.0), (30.0, 40.0)})
        report = await VoiceprintSampler(adapter, min_total_sec=5.0).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[
                _Seg(0.0, 2.0, "spk_0"),
                _Seg(10.0, 20.0, "spk_0"),
                _Seg(30.0, 40.0, "spk_0"),
            ],
        )
        assert report.candidates == ()
        assert "usable embeddings" in report.skipped_speakers["spk_0"]

    async def test_zero_length_segments_are_dropped(self) -> None:
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(adapter).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(5.0, 5.0, "spk_0"), _Seg(9.0, 8.0, "spk_0")],
        )
        assert report.candidates == ()
        assert adapter.calls == []


class TestCandidateShape:
    async def test_candidate_carries_linker_metadata(self) -> None:
        recorded_at = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
        adapter = _FakeVoiceprint()
        report = await VoiceprintSampler(adapter).sample(
            recording_id=42,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 10.0, "spk_0")],
            recorded_at=recorded_at,
            role_hints={"spk_0": "agent"},
            display_names={"spk_0": "王小姐"},
        )
        candidate = report.candidates[0]
        assert candidate.recording_id == 42
        assert candidate.first_seen == recorded_at
        assert candidate.role_hint == "agent"
        assert candidate.display_name == "王小姐"
        # voiceprint_id must be the hash the linker will re-derive.
        from audio_graphy.core.speaker_linker import hash_voiceprint

        assert candidate.voiceprint_id == hash_voiceprint(candidate.voiceprint)

    async def test_missing_role_hint_defaults_to_unknown(self) -> None:
        report = await VoiceprintSampler(_FakeVoiceprint()).sample(
            recording_id=1,
            audio_path="/tmp/a.wav",
            segments=[_Seg(0.0, 10.0, "spk_9")],
        )
        assert report.candidates[0].role_hint == "unknown"
        assert report.candidates[0].display_name == ""
