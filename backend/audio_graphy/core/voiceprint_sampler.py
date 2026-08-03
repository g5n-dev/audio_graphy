"""VoiceprintSampler — turn a diarized recording into speaker candidates.

This is the caller ``SpeakerLinker.run()`` documents but M7-M9 never shipped:
without it the whole voiceprint chain is inert (no ``_NewSpeakerCandidate``
is ever built, so no VoiceprintVector row is ever written by ingestion).

Input is the recording's own audio plus the per-segment speaker labels the
Chunker already derived from CAM++ diarization. Output is one
``_NewSpeakerCandidate`` per speaker that clears the quality gates.

Sampling strategy (ADR-0001)
----------------------------
``weighted_mean`` (default): extract one embedding per qualifying segment,
then average by duration and re-normalize. A mis-attributed segment only
contributes its share of the total, and the outlier pass can drop it
outright. ``longest_segment`` extracts a single embedding from the longest
segment — cheaper, but a single diarization error corrupts the candidate
completely, and in call-center audio the longest customer turn is often
only a couple of seconds.

Merged reception audio is never a valid input here: it interleaves several
speakers on one timeline, and CAM++ pools over all frames, so the resulting
vector represents nobody. Callers pass the original per-recording file.

Quality gates
-------------
Segments below ``min_segment_sec`` never contribute. A speaker whose
qualifying speech totals less than ``min_total_sec`` yields no candidate at
all — a 2-second sample is not a sound basis for merging identities across
recordings. ``max_segments`` caps extraction cost per speaker.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from audio_graphy.core.speaker_linker import _NewSpeakerCandidate, hash_voiceprint

if TYPE_CHECKING:
    from audio_graphy.adapters.protocols import VoiceprintAdapter

logger = logging.getLogger(__name__)

_DEFAULT_MIN_SEGMENT_SEC = 1.0
_DEFAULT_MIN_TOTAL_SEC = 3.0
_DEFAULT_MAX_SEGMENTS = 8
_DEFAULT_OUTLIER_COSINE = 0.5


class VoiceprintSamplingError(RuntimeError):
    """Every extraction attempt for one speaker failed.

    Distinct from "this speaker did not qualify": the audio was eligible,
    the voiceprint service could not process it.
    """


class _SpeakerSegment(Protocol):
    """Structural view of the segment fields the sampler needs.

    Read-only properties rather than plain attributes, so frozen dataclasses
    (``chunker.SegmentRecord``) satisfy it alongside the mutable ``Segment``
    ORM row — callers pass whichever they already hold.
    """

    @property
    def start_sec(self) -> float: ...

    @property
    def end_sec(self) -> float: ...

    @property
    def speaker(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class SpeakerSampleReport:
    """Why a recording produced the candidates it did.

    Attributes:
        recording_id: Source recording.
        candidates: One per speaker that cleared the gates.
        skipped_speakers: ``speaker_id -> reason`` for speakers that did not.
        extract_calls: Number of ``extract_voiceprint`` calls issued.
        dropped_outlier_segments: Segments discarded by the outlier pass.
    """

    recording_id: int
    candidates: tuple[_NewSpeakerCandidate, ...]
    skipped_speakers: dict[str, str]
    extract_calls: int = 0
    dropped_outlier_segments: int = 0


class VoiceprintSampler:
    """Build ``_NewSpeakerCandidate`` objects from a diarized recording.

    Args:
        voiceprint: Adapter exposing ``extract_voiceprint``.
        strategy: ``weighted_mean`` (default) or ``longest_segment``.
        min_segment_sec: Per-segment duration floor.
        min_total_sec: Per-speaker qualifying-speech floor.
        max_segments: Extraction cap per speaker (longest segments first).
        outlier_cosine: ``weighted_mean`` only — drop segments whose cosine
            against the first-pass centroid is below this, then recompute.
            ``0.0`` disables the pass.
    """

    def __init__(
        self,
        voiceprint: VoiceprintAdapter,
        *,
        strategy: str = "weighted_mean",
        min_segment_sec: float = _DEFAULT_MIN_SEGMENT_SEC,
        min_total_sec: float = _DEFAULT_MIN_TOTAL_SEC,
        max_segments: int = _DEFAULT_MAX_SEGMENTS,
        outlier_cosine: float = _DEFAULT_OUTLIER_COSINE,
    ) -> None:
        if strategy not in ("weighted_mean", "longest_segment"):
            raise ValueError(
                f"unknown voiceprint sampling strategy: {strategy!r} "
                "(expected 'weighted_mean' or 'longest_segment')"
            )
        if min_segment_sec <= 0 or min_total_sec <= 0:
            raise ValueError("sampling duration gates must be positive")
        if max_segments < 1:
            raise ValueError("max_segments must be ≥ 1")
        if not 0.0 <= outlier_cosine <= 1.0:
            raise ValueError("outlier_cosine must be in [0, 1]")
        self._voiceprint = voiceprint
        self._strategy = strategy
        self._min_segment_sec = min_segment_sec
        self._min_total_sec = min_total_sec
        self._max_segments = max_segments
        self._outlier_cosine = outlier_cosine

    async def sample(
        self,
        *,
        recording_id: int,
        audio_path: str,
        segments: Sequence[_SpeakerSegment],
        recorded_at: datetime | None = None,
        role_hints: dict[str, str] | None = None,
        display_names: dict[str, str] | None = None,
    ) -> SpeakerSampleReport:
        """Build one candidate per qualifying speaker in this recording.

        Args:
            recording_id: Source recording ID.
            audio_path: The recording's own audio file. Never a merged
                reception artifact (see module docstring).
            segments: Segments carrying diarization ``speaker`` labels.
                Segments with ``speaker=None`` are ignored.
            recorded_at: Recording timestamp → ``SpeakerNode.first_seen``.
            role_hints: ``speaker_id -> agent/customer/unknown``. Missing
                entries fall back to ``"unknown"``.
            display_names: ``speaker_id -> name`` for Layer-2 fuzzy matching.

        Returns:
            SpeakerSampleReport. A speaker that fails a gate is recorded in
            ``skipped_speakers`` rather than silently dropped.
        """
        by_speaker = self._group_by_speaker(segments)
        if not by_speaker:
            return SpeakerSampleReport(
                recording_id=recording_id,
                candidates=(),
                skipped_speakers={},
            )

        candidates: list[_NewSpeakerCandidate] = []
        skipped: dict[str, str] = {}
        extract_calls = 0
        dropped_outliers = 0

        for speaker_id, spk_segments in sorted(by_speaker.items()):
            # Total speech is reported over *all* of the speaker's segments:
            # it describes how much they talked, not how much we sampled.
            total_speech_sec = sum(s[1] - s[0] for s in spk_segments)

            usable = [s for s in spk_segments if (s[1] - s[0]) >= self._min_segment_sec]
            usable_sec = sum(s[1] - s[0] for s in usable)
            if usable_sec < self._min_total_sec:
                skipped[speaker_id] = (
                    f"insufficient usable speech: {usable_sec:.2f}s of segments ≥ "
                    f"{self._min_segment_sec}s (need {self._min_total_sec}s)"
                )
                continue

            # Longest first: the best-quality material, and the cap then
            # discards the weakest segments rather than an arbitrary slice.
            usable.sort(key=lambda s: s[1] - s[0], reverse=True)
            selected = usable[: self._max_segments]

            try:
                vector, calls, dropped, sampled_sec = await self._extract_candidate_vector(
                    audio_path=audio_path,
                    speaker_id=speaker_id,
                    selected=selected,
                )
            except Exception as exc:
                logger.warning(
                    "Voiceprint sampling failed for recording %d speaker %s: %s",
                    recording_id,
                    speaker_id,
                    exc,
                )
                skipped[speaker_id] = f"extraction failed: {exc}"
                continue

            extract_calls += calls
            dropped_outliers += dropped
            if vector is None:
                skipped[speaker_id] = "no usable embedding returned"
                continue
            # Re-check the gate against what actually contributed: failed or
            # discarded segments do not count, so a speaker that only passed
            # on paper must not produce a candidate.
            if sampled_sec < self._min_total_sec:
                skipped[speaker_id] = (
                    f"only {sampled_sec:.2f}s of audio produced usable embeddings "
                    f"(need {self._min_total_sec}s)"
                )
                continue

            candidates.append(
                _NewSpeakerCandidate(
                    speaker_id=speaker_id,
                    voiceprint=vector,
                    voiceprint_id=hash_voiceprint(vector),
                    recording_id=recording_id,
                    speech_sec=total_speech_sec,
                    sampled_sec=sampled_sec,
                    first_seen=recorded_at,
                    role_hint=(role_hints or {}).get(speaker_id, "unknown"),
                    display_name=(display_names or {}).get(speaker_id, ""),
                )
            )

        if skipped:
            logger.info(
                "Voiceprint sampling for recording %d skipped %d speaker(s): %s",
                recording_id,
                len(skipped),
                skipped,
            )
        return SpeakerSampleReport(
            recording_id=recording_id,
            candidates=tuple(candidates),
            skipped_speakers=skipped,
            extract_calls=extract_calls,
            dropped_outlier_segments=dropped_outliers,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _group_by_speaker(
        self,
        segments: Sequence[_SpeakerSegment],
    ) -> dict[str, list[tuple[float, float]]]:
        """Collect ``speaker_id -> [(start, end), ...]``, skipping unlabelled."""
        grouped: dict[str, list[tuple[float, float]]] = {}
        for seg in segments:
            speaker = getattr(seg, "speaker", None)
            if not speaker:
                continue
            start = float(seg.start_sec)
            end = float(seg.end_sec)
            if end <= start:
                continue
            grouped.setdefault(str(speaker), []).append((start, end))
        return grouped

    async def _extract_candidate_vector(
        self,
        *,
        audio_path: str,
        speaker_id: str,
        selected: list[tuple[float, float]],
    ) -> tuple[tuple[float, ...] | None, int, int, float]:
        """Return ``(vector, extract_calls, dropped_outliers, sampled_sec)``.

        ``sampled_sec`` counts only the audio that actually reached the
        centroid, so the caller can re-check its quality gate against
        reality rather than against the segments it hoped to use.
        """
        if self._strategy == "longest_segment":
            start, end = selected[0]  # already sorted longest-first
            result = await self._voiceprint.extract_voiceprint(
                audio_path,
                speaker_id=speaker_id,
                start_sec=start,
                end_sec=end,
            )
            vec = _l2_normalize(tuple(result.vector))
            return vec, 1, 0, (end - start) if vec is not None else 0.0

        # weighted_mean
        weighted: list[tuple[tuple[float, ...], float]] = []
        calls = 0
        failures = 0
        for start, end in selected:
            calls += 1
            try:
                result = await self._voiceprint.extract_voiceprint(
                    audio_path,
                    speaker_id=speaker_id,
                    start_sec=start,
                    end_sec=end,
                )
            except Exception as exc:
                # One bad window (a timeout, a slice the model rejects) must
                # not discard the embeddings that already succeeded. The
                # caller re-checks the total against min_total_sec, so losing
                # too much audio still fails the speaker honestly.
                failures += 1
                logger.warning(
                    "Voiceprint extraction failed for speaker %s window "
                    "[%.2f, %.2f]; continuing with the other segments: %s",
                    speaker_id,
                    start,
                    end,
                    exc,
                )
                continue
            vec = _l2_normalize(tuple(result.vector))
            if vec is not None:
                weighted.append((vec, end - start))

        if not weighted:
            if failures:
                raise VoiceprintSamplingError(
                    f"all {failures} extraction(s) failed for speaker {speaker_id}"
                )
            return None, calls, 0, 0.0

        centroid = _weighted_centroid(weighted)
        if centroid is None:
            return None, calls, 0, 0.0
        sampled_sec = sum(dur for _, dur in weighted)
        if self._outlier_cosine <= 0.0 or len(weighted) < 3:
            # With fewer than three samples there is no majority to judge an
            # outlier against — dropping one would just halve the evidence.
            return centroid, calls, 0, sampled_sec

        # Membership is decided one-segment-one-vote. Judging against the
        # duration-weighted centroid lets the longest segment dominate the
        # very average it is being tested against, so a mis-attributed long
        # segment would clear its own bar. Duration weighting still decides
        # the final template, just not who belongs in it.
        vote_centroid = _weighted_centroid([(vec, 1.0) for vec, _ in weighted])
        if vote_centroid is None:
            return centroid, calls, 0, sampled_sec

        kept = [
            (vec, dur)
            for vec, dur in weighted
            if _cosine(vec, vote_centroid) >= self._outlier_cosine
        ]
        dropped = len(weighted) - len(kept)
        if not kept or dropped == 0:
            return centroid, calls, 0, sampled_sec
        refined = _weighted_centroid(kept)
        if refined is None:
            return centroid, calls, 0, sampled_sec
        return refined, calls, dropped, sum(dur for _, dur in kept)


# ------------------------------------------------------------------
# Vector helpers
# ------------------------------------------------------------------
def _l2_normalize(vector: tuple[float, ...]) -> tuple[float, ...] | None:
    """L2-normalize; ``None`` for empty or degenerate vectors.

    The service contract says embeddings arrive normalized, but cosine and
    the ``voiceprint_id`` hash both assume unit norm, so normalize locally
    rather than trusting the wire.
    """
    if not vector:
        return None
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm < 1e-12:
        return None
    return tuple(float(x) / norm for x in vector)


def _weighted_centroid(
    weighted: Sequence[tuple[tuple[float, ...], float]],
) -> tuple[float, ...] | None:
    """Duration-weighted mean of L2-normalized vectors, re-normalized."""
    if not weighted:
        return None
    dim = len(weighted[0][0])
    if any(len(vec) != dim for vec, _ in weighted):
        raise ValueError("cannot average voiceprints of differing dimensionality")
    total_weight = sum(max(w, 0.0) for _, w in weighted)
    if total_weight <= 0.0:
        return None
    acc = [0.0] * dim
    for vec, weight in weighted:
        w = max(weight, 0.0)
        if w <= 0.0:
            continue
        for i, value in enumerate(vec):
            acc[i] += value * w
    return _l2_normalize(tuple(x / total_weight for x in acc))


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity for equal-length unit vectors."""
    if len(a) != len(b) or not a:
        return -1.0
    return sum(x * y for x, y in zip(a, b, strict=True))


__all__ = ["SpeakerSampleReport", "VoiceprintSampler", "VoiceprintSamplingError"]
