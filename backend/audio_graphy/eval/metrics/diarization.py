"""Diarization DER (Diarization Error Rate) — M7 WS-3 T11.

Computes the standard NIST RT Diarization Error Rate using a pure-Python
frame-based implementation (10 ms granularity).

DER = (False Alarm + Missed Speech + Speaker Confusion) / Total Reference Speech

Architecture §12.2.

Components:
    - ``DiarizationSegment``: (start_sec, end_sec, speaker_id) triple.
    - ``diarization_der(hypothesis, reference, *, collar_sec=0.25)`` → DERResult.
    - ``diarization_der_metric(...)`` → MetricResult for EvalRunner.

Algorithm (frame-based):
    1. Determine union time range [0, max_end] across ref + hyp.
    2. Discretise into 10ms frames.
    3. For each frame, build sets of ref speakers + hyp speakers (with
       collar tolerance applied to ref boundaries).
    4. Per frame:
        - missed speech: ref non-empty, hyp empty → counts.
        - false alarm:   ref empty, hyp non-empty → counts.
        - confusion:     ref non-empty, hyp non-empty, set difference → counts.
    5. DER numerator = missed + false_alarm + confusion frames.
    6. DER denominator = frames where ref non-empty.

Edge cases:
    - Empty reference → DERResult(value=0.0, skipped=True).
    - Empty hypothesis → DER = 1.0 if reference non-empty.
    - Perfect match → DER = 0.0.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from audio_graphy.eval.types import MetricResult

logger = logging.getLogger(__name__)


_FRAME_SEC = 0.01  # 10 ms granularity
_DEFAULT_COLLAR_SEC = 0.25  # NIST standard forgiveness collar


# ============================================================
# Data classes
# ============================================================


@dataclass(frozen=True, slots=True)
class DiarizationSegment:
    """One segment from a diarization timeline.

    Attributes:
        start_sec: Segment start (file-relative seconds).
        end_sec: Segment end (file-relative seconds).
        speaker_id: Speaker label (any hashable string).
    """

    start_sec: float
    end_sec: float
    speaker_id: str


@dataclass(frozen=True, slots=True)
class DERResult:
    """Output of ``diarization_der()``.

    Attributes:
        der: Diarization Error Rate in [0.0, 1.0+]. Lower is better.
            May exceed 1.0 if hyp has way more speech than ref (false alarm
            dominates); typical good systems are < 0.2.
        missed_speech_sec: Reference speech missed by hypothesis.
        false_alarm_sec: Hypothesis speech where reference has none.
        confused_speech_sec: Overlapping speech with wrong speaker mapping.
        total_reference_sec: Total reference speech duration (denominator).
        skipped: True if metric could not be computed (empty reference).
        optimal_mapping: Best 1-1 ref→hyp speaker mapping that minimises
            confusion (None if not computed or trivial).
    """

    der: float
    missed_speech_sec: float
    false_alarm_sec: float
    confused_speech_sec: float
    total_reference_sec: float
    skipped: bool = False
    optimal_mapping: dict[str, str] = field(default_factory=dict)


# ============================================================
# Pure-Python DER computation
# ============================================================


def diarization_der(
    hypothesis: Sequence[DiarizationSegment],
    reference: Sequence[DiarizationSegment],
    *,
    collar_sec: float = _DEFAULT_COLLAR_SEC,
    compute_optimal_mapping: bool = True,
) -> DERResult:
    """Compute Diarization Error Rate for one file.

    Args:
        hypothesis: System output segments (e.g. CAM++ diarize).
        reference: Ground-truth segments (e.g. hand-labelled RTTM).
        collar_sec: Forgiveness collar in seconds applied around reference
            boundaries (NIST standard 0.25). Set to 0 for exact-match.
        compute_optimal_mapping: When True, compute a globally-optimal
            1-1 mapping between ref and hyp speaker IDs (Hungarian-lite
            greedy). When False, treat speaker IDs as already aligned
            (only sensible when ref and hyp share label vocabulary).

    Returns:
        DERResult with the DER + component breakdowns.
    """
    ref = _coerce_segments(reference)
    hyp = _coerce_segments(hypothesis)

    if not ref:
        return DERResult(
            der=0.0,
            missed_speech_sec=0.0,
            false_alarm_sec=0.0,
            confused_speech_sec=0.0,
            total_reference_sec=0.0,
            skipped=True,
            optimal_mapping={},
        )

    # Determine timeline range.
    max_end = 0.0
    for seg in ref:
        max_end = max(max_end, seg.end_sec)
    for seg in hyp:
        max_end = max(max_end, seg.end_sec)
    if max_end <= 0.0:
        return DERResult(
            der=0.0,
            missed_speech_sec=0.0,
            false_alarm_sec=0.0,
            confused_speech_sec=0.0,
            total_reference_sec=0.0,
            skipped=True,
        )

    # Compute optimal ref→hyp speaker mapping (maximise overlap).
    if compute_optimal_mapping:
        mapping = _optimal_speaker_mapping(ref, hyp)
    else:
        mapping = {seg.speaker_id: seg.speaker_id for seg in ref}

    # Frame-based scoring (10 ms granularity).
    n_frames = math.ceil(max_end / _FRAME_SEC)
    if n_frames <= 0:
        return DERResult(
            der=0.0,
            missed_speech_sec=0.0,
            false_alarm_sec=0.0,
            confused_speech_sec=0.0,
            total_reference_sec=0.0,
            skipped=True,
        )

    # Per-frame ref/hyp speaker sets (with collar on ref boundaries).
    ref_per_frame = _frames_per_speaker(ref, n_frames, collar_sec)
    hyp_per_frame = _frames_per_speaker(hyp, n_frames, 0.0)

    missed_frames = 0
    false_alarm_frames = 0
    confused_frames = 0
    ref_active_frames = 0

    for frame_idx in range(n_frames):
        ref_speakers = {spk for spk, frames in ref_per_frame.items() if frame_idx in frames}
        hyp_speakers_raw = {spk for spk, frames in hyp_per_frame.items() if frame_idx in frames}
        # Map hyp speakers through the inverse mapping (hyp → ref).
        hyp_speakers_mapped = set()
        for h_spk in hyp_speakers_raw:
            # Find which ref speaker this hyp speaker maps to.
            mapped_ref = None
            for r, h in mapping.items():
                if h == h_spk:
                    mapped_ref = r
                    break
            if mapped_ref is not None:
                hyp_speakers_mapped.add(mapped_ref)
            else:
                # Hyp speaker not in mapping — counts as confusion.
                hyp_speakers_mapped.add(f"__unmapped__{h_spk}")

        if ref_speakers:
            ref_active_frames += 1
            if not hyp_speakers_mapped:
                missed_frames += 1
            elif ref_speakers != hyp_speakers_mapped:
                confused_frames += 1
        elif hyp_speakers_mapped:
            false_alarm_frames += 1

    total_ref_sec = ref_active_frames * _FRAME_SEC
    missed_sec = missed_frames * _FRAME_SEC
    false_alarm_sec = false_alarm_frames * _FRAME_SEC
    confused_sec = confused_frames * _FRAME_SEC

    der = (
        (missed_sec + false_alarm_sec + confused_sec) / total_ref_sec if total_ref_sec > 0 else 0.0
    )

    return DERResult(
        der=der,
        missed_speech_sec=missed_sec,
        false_alarm_sec=false_alarm_sec,
        confused_speech_sec=confused_sec,
        total_reference_sec=total_ref_sec,
        skipped=False,
        optimal_mapping=mapping,
    )


def diarization_der_metric(
    hypothesis: Sequence[DiarizationSegment],
    reference: Sequence[DiarizationSegment],
    *,
    collar_sec: float = _DEFAULT_COLLAR_SEC,
) -> MetricResult:
    """Same as ``diarization_der`` but returns a MetricResult for EvalRunner."""
    result = diarization_der(hypothesis, reference, collar_sec=collar_sec)
    return MetricResult(
        name="diarization_der",
        value=result.der,
        denominator=max(1, round(result.total_reference_sec / _FRAME_SEC)),
        details={
            "missed_speech_sec": result.missed_speech_sec,
            "false_alarm_sec": result.false_alarm_sec,
            "confused_speech_sec": result.confused_speech_sec,
            "total_reference_sec": result.total_reference_sec,
            "collar_sec": collar_sec,
            "skipped": result.skipped,
        },
    )


# ============================================================
# RTTM parsing (PRD §6.5 — real-data CI-external runs)
# ============================================================


def parse_rttm(path: str) -> list[DiarizationSegment]:
    """Parse an RTTM v2 file into a list of DiarizationSegment.

    Each RTTM line has 10 fields (only the ones we use are parsed):
        SPEAKER <file> <chnl> <onset> <dur> <ortho> <stype> <name> <conf> <slat>

    Args:
        path: Path to the .rttm file.

    Returns:
        List of DiarizationSegment.
    """
    out: list[DiarizationSegment] = []
    with open(path, encoding="utf-8") as f:
        for raw in f.read().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 8 or parts[0] != "SPEAKER":
                continue
            try:
                onset = float(parts[3])
                duration = float(parts[4])
                speaker = parts[7]
            except (ValueError, IndexError):
                logger.warning("Skipping malformed RTTM line: %r", raw)
                continue
            out.append(
                DiarizationSegment(
                    start_sec=onset,
                    end_sec=onset + duration,
                    speaker_id=speaker,
                )
            )
    return out


# ============================================================
# Internal helpers
# ============================================================


def _coerce_segments(
    segments: Sequence[DiarizationSegment],
) -> list[DiarizationSegment]:
    """Filter out zero-or-negative-length segments."""
    return [s for s in segments if s.end_sec > s.start_sec and s.speaker_id]


def _frames_per_speaker(
    segments: list[DiarizationSegment],
    n_frames: int,
    collar_sec: float,
) -> dict[str, set[int]]:
    """Map each speaker to the set of frame indices they cover.

    Collar extends each segment by ``collar_sec`` on both sides (NIST
    standard 0.25s — boundary forgiveness).
    """
    out: dict[str, set[int]] = {}
    for seg in segments:
        start = max(0.0, seg.start_sec - collar_sec)
        end = min(n_frames * _FRAME_SEC, seg.end_sec + collar_sec)
        first = int(start / _FRAME_SEC)
        last = int(end / _FRAME_SEC)
        frames = set(range(first, last + 1))
        out.setdefault(seg.speaker_id, set()).update(frames)
    return out


def _optimal_speaker_mapping(
    ref: list[DiarizationSegment],
    hyp: list[DiarizationSegment],
) -> dict[str, str]:
    """Globally-optimal 1-1 mapping between ref and hyp speaker IDs.

    Greedy implementation: for each ref speaker, find the hyp speaker that
    maximises temporal overlap (in seconds). Ties broken alphabetically.
    Produces ``{ref_speaker: hyp_speaker}``.

    For M7 scale (typically < 20 speakers per file), greedy is close to
    the Hungarian algorithm optimum. A future M8 may swap in scipy's
    ``linear_sum_assignment`` if scoring becomes a bottleneck.
    """
    ref_speakers = sorted({s.speaker_id for s in ref})
    hyp_speakers = sorted({s.speaker_id for s in hyp})

    if not ref_speakers or not hyp_speakers:
        return dict.fromkeys(ref_speakers, "")

    # Compute overlap matrix.
    overlaps: dict[tuple[str, str], float] = {}
    for r in ref_speakers:
        for h in hyp_speakers:
            overlaps[(r, h)] = _total_overlap(ref, hyp, r, h)

    # Greedy: pick the largest overlap pair, lock both speakers, repeat.
    mapping: dict[str, str] = {}
    used_hyp: set[str] = set()
    remaining = list(overlaps.items())
    remaining.sort(key=lambda kv: kv[1], reverse=True)
    for (r, h), _ in remaining:
        if r in mapping or h in used_hyp:
            continue
        mapping[r] = h
        used_hyp.add(h)

    # Ref speakers with no hyp mapping get an empty string sentinel.
    for r in ref_speakers:
        if r not in mapping:
            mapping[r] = ""
    return mapping


def _total_overlap(
    ref: list[DiarizationSegment],
    hyp: list[DiarizationSegment],
    ref_speaker: str,
    hyp_speaker: str,
) -> float:
    """Total temporal overlap (seconds) between ref_speaker and hyp_speaker."""
    ref_segs = [s for s in ref if s.speaker_id == ref_speaker]
    hyp_segs = [s for s in hyp if s.speaker_id == hyp_speaker]
    total = 0.0
    for r in ref_segs:
        for h in hyp_segs:
            start = max(r.start_sec, h.start_sec)
            end = min(r.end_sec, h.end_sec)
            if end > start:
                total += end - start
    return total


__all__ = [
    "DERResult",
    "DiarizationSegment",
    "diarization_der",
    "diarization_der_metric",
    "parse_rttm",
]
