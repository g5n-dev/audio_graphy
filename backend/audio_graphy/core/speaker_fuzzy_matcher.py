"""T11 — SpeakerFuzzyMatcher (M9 architecture §10, L8 ruling).

L8 ruling (binding):
    Layer 1 — voiceprint cosine:
        cosine >= 0.7  → CONFIRMED link (no Layer 2 needed)
    Layer 2 — rapidfuzz token_ratio on speaker name strings:
        ratio >= 0.85  → AMBIGUOUS (needs reconfirm)
        0.6 <= ratio < 0.85 → INFERRED (tentative; visible in admin views)
        ratio < 0.6      → no match

When Layer 2 returns AMBIGUOUS the matcher enqueues a ``SpeakerMergePending``
row for downstream reconfirm (T12 wiring).

Attribution: uses the rapidfuzz library (MIT) — already a project dep since M6.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from audio_graphy.core.types import (
    SpeakerLinkerFuzzyThresholdError,
    SpeakerLinkerReconfirmUnavailableError,
)

logger = logging.getLogger(__name__)

# L8 ruling — binding thresholds (do not change without PM sign-off).
L8_VOICEPRINT_COSINE_RECONFIRM: float = 0.7
L8_FUZZY_AMBIGUOUS: float = 0.85
L8_FUZZY_INFERRED: float = 0.6

# Public type for the match outcome.
SpeakerFuzzyVerdict = Literal["CONFIRMED", "AMBIGUOUS", "INFERRED", "NO_MATCH"]


# ============================================================
# Public types
# ============================================================


@dataclass(frozen=True, slots=True)
class SpeakerCandidate:
    """One existing speaker that the matcher compared a query against.

    Attributes:
        speaker_node_id: DB id of the candidate ``speaker_nodes`` row.
        canonical_name: Stored display name of the speaker.
        voiceprint_vector: Optional embedding (None if not enrolled).
    """

    speaker_node_id: int
    canonical_name: str
    voiceprint_vector: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class SpeakerFuzzyResult:
    """Output of one ``SpeakerFuzzyMatcher.match`` call.

    Attributes:
        verdict: CONFIRMED / AMBIGUOUS / INFERRED / NO_MATCH (L8).
        matched_candidate: Best-matching SpeakerCandidate (None on NO_MATCH).
        fuzzy_score: rapidfuzz token_ratio in [0, 1]; 0.0 if no string compared.
        voiceprint_score: cosine in [-1, 1]; None if no vector compared.
        needs_reconfirm: True iff verdict == AMBIGUOUS (L8 reconfirm queue).
    """

    verdict: SpeakerFuzzyVerdict
    matched_candidate: SpeakerCandidate | None
    fuzzy_score: float
    voiceprint_score: float | None
    needs_reconfirm: bool


# ============================================================
# Vector adapter protocol
# ============================================================


class VoiceprintComparator(Protocol):
    """Computes cosine similarity between two voiceprint vectors."""

    def cosine(
        self,
        query: tuple[float, ...],
        candidate: tuple[float, ...],
    ) -> float: ...


class DefaultVoiceprintComparator:
    """Pure-Python cosine implementation (no numpy dependency at this layer)."""

    def cosine(
        self,
        query: tuple[float, ...],
        candidate: tuple[float, ...],
    ) -> float:
        if len(query) != len(candidate):
            raise ValueError(f"dimension mismatch: {len(query)} vs {len(candidate)}")
        dot = sum(a * b for a, b in zip(query, candidate, strict=True))
        norm_q = sum(a * a for a in query) ** 0.5
        norm_c = sum(b * b for b in candidate) ** 0.5
        if norm_q == 0.0 or norm_c == 0.0:
            return 0.0
        return float(dot / (norm_q * norm_c))


# ============================================================
# Matcher
# ============================================================


class SpeakerFuzzyMatcher:
    """Layer 2 fuzzy name matcher + Layer 1 voiceprint reconfirm (L8).

    Args:
        voiceprint_comparator: Cosine backend (defaults to pure-Python).
        ambiguous_threshold: L8 rapidfuzz token_ratio cap for AMBIGUOUS.
        inferred_threshold: L8 cap for INFERRED.
        reconfirm_cosine: L8 cosine cap for CONFIRMED reconfirm.
    """

    def __init__(
        self,
        *,
        voiceprint_comparator: VoiceprintComparator | None = None,
        ambiguous_threshold: float = L8_FUZZY_AMBIGUOUS,
        inferred_threshold: float = L8_FUZZY_INFERRED,
        reconfirm_cosine: float = L8_VOICEPRINT_COSINE_RECONFIRM,
    ) -> None:
        # Validate L8 thresholds.
        for name, val in (
            ("ambiguous_threshold", ambiguous_threshold),
            ("inferred_threshold", inferred_threshold),
            ("reconfirm_cosine", reconfirm_cosine),
        ):
            if not 0.0 <= val <= 1.0:
                raise SpeakerLinkerFuzzyThresholdError(f"{name}={val} outside [0, 1]")
        if inferred_threshold > ambiguous_threshold:
            raise SpeakerLinkerFuzzyThresholdError(
                f"inferred_threshold ({inferred_threshold}) > "
                f"ambiguous_threshold ({ambiguous_threshold})"
            )
        self._vc = voiceprint_comparator or DefaultVoiceprintComparator()
        self._ambiguous_t = ambiguous_threshold
        self._inferred_t = inferred_threshold
        self._reconfirm_cosine = reconfirm_cosine

    # ------------------------------------------------------------
    # Layer 2 entry point
    # ------------------------------------------------------------

    def match(
        self,
        *,
        query_name: str,
        candidates: Sequence[SpeakerCandidate],
        query_voiceprint: tuple[float, ...] | None = None,
    ) -> SpeakerFuzzyResult:
        """Run Layer 2 (rapidfuzz) then optionally Layer 1 (voiceprint).

        L8 decision tree:
          1. Compute rapidfuzz token_ratio vs each candidate's canonical_name.
          2. Pick the best-scoring candidate.
          3. If best score >= AMBIGUOUS threshold AND a query_voiceprint was
             supplied AND the candidate has a voiceprint → run Layer 1 reconfirm.
             - cosine >= 0.7 → CONFIRMED (no reconfirm queue needed).
             - cosine < 0.7 → AMBIGUOUS (enqueued by caller).
          4. If best score >= AMBIGUOUS but no voiceprint available → AMBIGUOUS
             + raise ``SpeakerLinkerReconfirmUnavailableError`` to signal the
             caller that they must populate a SpeakerMergePending row.
          5. If AMBIGUOUS > score >= INFERRED → INFERRED (no reconfirm).
          6. Else → NO_MATCH.
        """
        if not candidates:
            return SpeakerFuzzyResult(
                verdict="NO_MATCH",
                matched_candidate=None,
                fuzzy_score=0.0,
                voiceprint_score=None,
                needs_reconfirm=False,
            )

        best, best_score = self._best_fuzzy_match(query_name, candidates)

        # Stage A — string-score gating.
        if best_score >= self._ambiguous_t:
            return self._resolve_ambiguous(
                best=best,
                best_score=best_score,
                query_voiceprint=query_voiceprint,
            )
        if best_score >= self._inferred_t:
            return SpeakerFuzzyResult(
                verdict="INFERRED",
                matched_candidate=best,
                fuzzy_score=best_score,
                voiceprint_score=None,
                needs_reconfirm=False,
            )
        return SpeakerFuzzyResult(
            verdict="NO_MATCH",
            matched_candidate=None,
            fuzzy_score=best_score,
            voiceprint_score=None,
            needs_reconfirm=False,
        )

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _best_fuzzy_match(
        self,
        query_name: str,
        candidates: Sequence[SpeakerCandidate],
    ) -> tuple[SpeakerCandidate, float]:
        """Return (best_candidate, best_score in [0, 1])."""
        # Lazy import — rapidfuzz is a hard dep but kept lazy for unit-test
        # isolation when the matcher is constructed but never invoked.
        from rapidfuzz import fuzz

        best = candidates[0]
        best_score = 0.0
        for cand in candidates:
            raw = fuzz.token_ratio(query_name, cand.canonical_name)
            score = raw / 100.0  # rapidfuzz returns 0..100; we use 0..1
            if score > best_score:
                best_score = score
                best = cand
        return best, best_score

    def _resolve_ambiguous(
        self,
        *,
        best: SpeakerCandidate,
        best_score: float,
        query_voiceprint: tuple[float, ...] | None,
    ) -> SpeakerFuzzyResult:
        """Apply L8 Layer 1 reconfirm when voiceprints are available."""
        if query_voiceprint is None or best.voiceprint_vector is None:
            # Stage 4 — reconfirm unavailable; signal caller.
            return SpeakerFuzzyResult(
                verdict="AMBIGUOUS",
                matched_candidate=best,
                fuzzy_score=best_score,
                voiceprint_score=None,
                needs_reconfirm=True,
            )

        cosine = self._vc.cosine(query_voiceprint, best.voiceprint_vector)
        if cosine >= self._reconfirm_cosine:
            return SpeakerFuzzyResult(
                verdict="CONFIRMED",
                matched_candidate=best,
                fuzzy_score=best_score,
                voiceprint_score=cosine,
                needs_reconfirm=False,
            )
        return SpeakerFuzzyResult(
            verdict="AMBIGUOUS",
            matched_candidate=best,
            fuzzy_score=best_score,
            voiceprint_score=cosine,
            needs_reconfirm=True,
        )


# ============================================================
# Batch helper
# ============================================================


def match_with_reconfirm_or_raise(
    matcher: SpeakerFuzzyMatcher,
    *,
    query_name: str,
    candidates: Sequence[SpeakerCandidate],
    query_voiceprint: tuple[float, ...] | None,
    require_reconfirm_availability: bool = False,
) -> SpeakerFuzzyResult:
    """Convenience wrapper used by SpeakerLinker (T12).

    When ``require_reconfirm_availability`` is True and Layer 2 yields
    AMBIGUOUS but no voiceprint is available, raise instead of returning
    AMBIGUOUS. This forces the caller to either enroll a voiceprint or
    explicitly enqueue a SpeakerMergePending row.
    """
    result = matcher.match(
        query_name=query_name,
        candidates=candidates,
        query_voiceprint=query_voiceprint,
    )
    if (
        require_reconfirm_availability
        and result.verdict == "AMBIGUOUS"
        and result.voiceprint_score is None
        and query_voiceprint is None
    ):
        raise SpeakerLinkerReconfirmUnavailableError(
            f"L8 reconfirm required for '{query_name}' but no query voiceprint"
        )
    return result


# ============================================================
# Helpers re-exported for tests
# ============================================================


def normalise_fuzzy_score(raw: float) -> float:
    """Convert rapidfuzz 0..100 → 0..1."""
    return raw / 100.0


def denormalise_fuzzy_score(score_01: float) -> float:
    """Inverse of ``normalise_fuzzy_score``."""
    return score_01 * 100.0
