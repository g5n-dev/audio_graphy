"""Voiceprint EER (Equal Error Rate) — M7 WS-3 T11.

Computes the Equal Error Rate: the threshold point at which the False
Accept Rate (FAR) equals the False Reject Rate (FRR). Lower EER = better.

Architecture §12.1.

Algorithm:
    1. Sort all cosines (same-speaker + diff-speaker) in descending order.
    2. Sweep each unique cosine value as a candidate threshold.
    3. Compute (FAR, FRR) at each threshold.
    4. EER is the point where |FAR - FRR| is minimum (ties averaged per
       §17 shared knowledge).
    5. Report EER, the threshold at which it occurs, and the full ROC curve.

Two API surfaces:
    - ``voiceprint_eer(same_speaker_cosines, diff_speaker_cosines)`` —
      pure-Python EER over precomputed cosine values. Fast, deterministic,
      no I/O.
    - ``voiceprint_eer_from_trials(trials, adapter)`` — async; takes trial
      pairs (paths + same/diff label) and a VoiceprintAdapter, extracts
      voiceprints, computes cosines, then calls ``voiceprint_eer``.

Edge cases:
    - Empty inputs → returns ``EERResult(value=0.0, ...)`` with
      ``details["skipped"]=True`` (denominator 0).
    - All-same (perfect) → EER = 0.0.
    - All-different (perfect) → EER = 0.0.
    - Single point in one set → still works (degenerate but defined).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_graphy.eval.types import MetricResult

logger = logging.getLogger(__name__)


# ============================================================
# Pure-Python EER (operates on precomputed cosines)
# ============================================================


@dataclass(frozen=True, slots=True)
class EERResult:
    """Output of ``voiceprint_eer()``.

    Attributes:
        eer: Equal Error Rate in [0.0, 1.0]. Lower is better.
        threshold: Cosine threshold at which FAR == FRR (None when undefined).
        far_at_eer: False Accept Rate at the EER point.
        frr_at_eer: False Reject Rate at the EER point.
        same_speaker_count: Number of same-speaker trials.
        diff_speaker_count: Number of diff-speaker trials.
        roc_curve: Optional list of (threshold, far, frr) tuples
            (subsampled to ≤ 101 points for reporting).
        skipped: True if metric could not be computed (empty inputs).
    """

    eer: float
    threshold: float | None
    far_at_eer: float
    frr_at_eer: float
    same_speaker_count: int
    diff_speaker_count: int
    roc_curve: tuple[tuple[float, float, float], ...] = field(default_factory=tuple)
    skipped: bool = False


def voiceprint_eer(
    same_speaker_cosines: Sequence[float],
    diff_speaker_cosines: Sequence[float],
    *,
    roc_samples: int = 101,
) -> EERResult:
    """Compute Equal Error Rate from precomputed cosine similarities.

    Args:
        same_speaker_cosines: Cosines between voiceprint pairs that share
            the same speaker (positive trials).
        diff_speaker_cosines: Cosines between voiceprint pairs from
            different speakers (negative trials).
        roc_samples: Maximum number of (threshold, far, frr) tuples to
            keep in ``EERResult.roc_curve`` for reporting. Default 101.

    Returns:
        EERResult with the EER and metadata.

    Implementation notes (§17 shared knowledge — ties handling):
        When multiple thresholds produce the same |FAR - FRR|, we average
        the FAR/FRR values across the tie group. This handles the common
        case where cosines have discrete values (e.g. mock-mode hashes).
    """
    same = [float(x) for x in same_speaker_cosines if math.isfinite(float(x))]
    diff = [float(x) for x in diff_speaker_cosines if math.isfinite(float(x))]

    if not same or not diff:
        return EERResult(
            eer=0.0,
            threshold=None,
            far_at_eer=0.0,
            frr_at_eer=0.0,
            same_speaker_count=len(same),
            diff_speaker_count=len(diff),
            skipped=True,
        )

    n_same = len(same)
    n_diff = len(diff)

    # Candidate thresholds: unique cosines from both sets, sorted descending.
    # Add ±inf sentinels so the curve covers the full FAR/FRR range.
    sorted_values = sorted(set(same) | set(diff), reverse=True)
    thresholds: list[float] = [math.inf]
    thresholds.extend(sorted_values)
    thresholds.append(-math.inf)

    # Sweep: accept pair iff cosine >= threshold.
    # FAR  = #diff accepted / n_diff   (lower is better)
    # FRR  = #same rejected / n_same   (lower is better)
    # We compute FAR/FRR at each threshold by counting.
    best_eer = math.inf
    best_threshold: float | None = None
    best_far = 0.0
    best_frr = 0.0
    roc_points: list[tuple[float, float, float]] = []

    for thr in thresholds:
        far = sum(1 for c in diff if c >= thr) / n_diff
        frr = sum(1 for c in same if c < thr) / n_same
        roc_points.append((thr if math.isfinite(thr) else (1.0 if thr > 0 else 0.0), far, frr))

        eer_at_thr = (far + frr) / 2.0
        if abs(far - frr) < best_eer - eer_at_thr or (
            abs(abs(far - frr) - (best_eer - eer_at_thr)) < 1e-12
            and eer_at_thr < best_eer
        ):
            best_eer = eer_at_thr
            best_threshold = thr if math.isfinite(thr) else None
            best_far = far
            best_frr = frr

    # Ties handling: average FAR/FRR over all thresholds that achieve the
    # best |FAR - FRR| value (within epsilon).
    best_diff = abs(best_far - best_frr)
    tie_far_sum = 0.0
    tie_frr_sum = 0.0
    tie_count = 0
    for _thr, far, frr in roc_points:
        if abs(abs(far - frr) - best_diff) < 1e-9:
            tie_far_sum += far
            tie_frr_sum += frr
            tie_count += 1
    if tie_count > 1:
        best_far = tie_far_sum / tie_count
        best_frr = tie_frr_sum / tie_count
        best_eer = (best_far + best_frr) / 2.0

    # Subsample ROC curve for reporting (≤ roc_samples points).
    if len(roc_points) > roc_samples:
        step = len(roc_points) / roc_samples
        sampled = [roc_points[int(i * step)] for i in range(roc_samples)]
    else:
        sampled = roc_points

    return EERResult(
        eer=best_eer,
        threshold=best_threshold,
        far_at_eer=best_far,
        frr_at_eer=best_frr,
        same_speaker_count=n_same,
        diff_speaker_count=n_diff,
        roc_curve=tuple(sampled),
        skipped=False,
    )


def voiceprint_eer_metric(
    same_speaker_cosines: Sequence[float],
    diff_speaker_cosines: Sequence[float],
) -> MetricResult:
    """Same as ``voiceprint_eer`` but returns a MetricResult for EvalRunner.

    The metric name is ``"voiceprint_eer"`` and the value is the EER ∈ [0,1].
    """
    eer = voiceprint_eer(same_speaker_cosines, diff_speaker_cosines)
    return MetricResult(
        name="voiceprint_eer",
        value=eer.eer,
        denominator=max(1, eer.same_speaker_count + eer.diff_speaker_count),
        details={
            "threshold": eer.threshold if eer.threshold is not None else 0.0,
            "far_at_eer": eer.far_at_eer,
            "frr_at_eer": eer.frr_at_eer,
            "same_speaker_count": eer.same_speaker_count,
            "diff_speaker_count": eer.diff_speaker_count,
            "skipped": eer.skipped,
        },
    )


# ============================================================
# Trial-file loader + async EER (calls VoiceprintAdapter)
# ============================================================


@dataclass(frozen=True, slots=True)
class VoiceprintTrial:
    """One voiceprint verification trial (CN-Celeb-style).

    Attributes:
        enrollment_path: Path to the enrollment audio file.
        test_path: Path to the test audio file.
        same_speaker: True if both files are from the same speaker.
    """

    enrollment_path: str
    test_path: str
    same_speaker: bool


def parse_trial_file(trial_path: Path) -> list[VoiceprintTrial]:
    """Parse a CN-Celeb-style trial file.

    Each line: ``enrollment_path test_path 0|1`` (0 = different speaker,
    1 = same speaker). Lines starting with ``#`` and empty lines are
    skipped.

    Args:
        trial_path: Path to the trial file.

    Returns:
        List of VoiceprintTrial objects.
    """
    trials: list[VoiceprintTrial] = []
    for raw in trial_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            logger.warning("Skipping malformed trial line: %r", raw)
            continue
        try:
            label = int(parts[2])
        except ValueError:
            logger.warning("Skipping trial with non-int label: %r", raw)
            continue
        trials.append(
            VoiceprintTrial(
                enrollment_path=parts[0],
                test_path=parts[1],
                same_speaker=bool(label),
            )
        )
    return trials


async def voiceprint_eer_from_trials(
    trials: Sequence[VoiceprintTrial],
    adapter: Any,
) -> EERResult:
    """Extract voiceprints via ``adapter`` then compute EER.

    Caches voiceprints per unique audio path to avoid re-extraction.

    Args:
        trials: List of VoiceprintTrial objects.
        adapter: VoiceprintAdapter (must implement ``extract_voiceprint``).

    Returns:
        EERResult.
    """
    if not trials:
        return voiceprint_eer([], [])

    # Cache: path → voiceprint tuple.
    cache: dict[str, tuple[float, ...]] = {}
    same_cosines: list[float] = []
    diff_cosines: list[float] = []

    async def _get_vec(path: str) -> tuple[float, ...] | None:
        if path in cache:
            return cache[path]
        try:
            result = await adapter.extract_voiceprint(path)
            vec = tuple(float(x) for x in result.vector)
            cache[path] = vec
            return vec
        except Exception as exc:
            logger.warning("Voiceprint extraction failed for %s: %s", path, exc)
            return None

    for trial in trials:
        v1 = await _get_vec(trial.enrollment_path)
        v2 = await _get_vec(trial.test_path)
        if v1 is None or v2 is None:
            continue
        cos = _cosine(v1, v2)
        if trial.same_speaker:
            same_cosines.append(cos)
        else:
            diff_cosines.append(cos)

    return voiceprint_eer(same_cosines, diff_cosines)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity for equal-length vectors (handles un-normalised)."""
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-12 or norm_b < 1e-12:
        return -1.0
    return dot / (norm_a * norm_b)


__all__ = [
    "EERResult",
    "VoiceprintTrial",
    "parse_trial_file",
    "voiceprint_eer",
    "voiceprint_eer_from_trials",
    "voiceprint_eer_metric",
]
