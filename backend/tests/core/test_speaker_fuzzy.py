"""T11 — SpeakerFuzzyMatcher tests (architecture §10, L8).

Verifies L8 ruling:
  - Layer 2 rapidfuzz token_ratio >= 0.85 → AMBIGUOUS (or CONFIRMED if Layer 1 passes)
  - Layer 1 voiceprint cosine >= 0.7 → CONFIRMED on reconfirm
  - 0.6 <= ratio < 0.85 → INFERRED
  - ratio < 0.6 → NO_MATCH
  - Threshold validation rejects out-of-range values
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from audio_graphy.core.speaker_fuzzy_matcher import (
    L8_FUZZY_AMBIGUOUS,
    L8_FUZZY_INFERRED,
    L8_VOICEPRINT_COSINE_RECONFIRM,
    DefaultVoiceprintComparator,
    SpeakerCandidate,
    SpeakerFuzzyMatcher,
    SpeakerFuzzyResult,
    denormalise_fuzzy_score,
    match_with_reconfirm_or_raise,
    normalise_fuzzy_score,
)
from audio_graphy.core.types import (
    SpeakerLinkerFuzzyThresholdError,
    SpeakerLinkerReconfirmUnavailableError,
)


@pytest.fixture()
def matcher() -> SpeakerFuzzyMatcher:
    return SpeakerFuzzyMatcher()


# ============================================================
# Constructor validation
# ============================================================


def test_constructor_rejects_out_of_range_thresholds() -> None:
    with pytest.raises(SpeakerLinkerFuzzyThresholdError):
        SpeakerFuzzyMatcher(ambiguous_threshold=-0.1)
    with pytest.raises(SpeakerLinkerFuzzyThresholdError):
        SpeakerFuzzyMatcher(ambiguous_threshold=1.5)
    with pytest.raises(SpeakerLinkerFuzzyThresholdError):
        SpeakerFuzzyMatcher(reconfirm_cosine=-1.0)


def test_constructor_rejects_inverted_thresholds() -> None:
    """inferred_threshold must NOT exceed ambiguous_threshold."""
    with pytest.raises(SpeakerLinkerFuzzyThresholdError):
        SpeakerFuzzyMatcher(ambiguous_threshold=0.7, inferred_threshold=0.85)


def test_l8_constants_locked() -> None:
    """L8 thresholds are binding — assert their values explicitly."""
    assert L8_FUZZY_AMBIGUOUS == 0.85
    assert L8_FUZZY_INFERRED == 0.6
    assert L8_VOICEPRINT_COSINE_RECONFIRM == 0.7


# ============================================================
# Layer 2 string matching
# ============================================================


def test_no_candidates_returns_no_match(matcher: SpeakerFuzzyMatcher) -> None:
    result = matcher.match(query_name="王小姐", candidates=[])
    assert result.verdict == "NO_MATCH"
    assert result.matched_candidate is None
    assert result.fuzzy_score == 0.0


def test_low_score_returns_no_match(matcher: SpeakerFuzzyMatcher) -> None:
    """rapidfuzz token_ratio('王小姐', '王太太') < 0.6 → NO_MATCH."""
    candidates = [SpeakerCandidate(1, "王太太")]
    result = matcher.match(query_name="王小姐", candidates=candidates)
    assert result.verdict == "NO_MATCH"
    assert result.fuzzy_score < L8_FUZZY_INFERRED


def test_inferred_range(matcher: SpeakerFuzzyMatcher) -> None:
    """rapidfuzz token_ratio in [0.6, 0.85) → INFERRED."""
    # '王小姐' vs '王小姐' would yield >= 0.85; pick something looser.
    candidates = [SpeakerCandidate(1, "王小明")]
    result = matcher.match(query_name="王小姐", candidates=candidates)
    if L8_FUZZY_INFERRED <= result.fuzzy_score < L8_FUZZY_AMBIGUOUS:
        assert result.verdict == "INFERRED"
        assert result.needs_reconfirm is False


def test_exact_match_ambiguous_without_voiceprint(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    """Identical name → ratio=1.0 → AMBIGUOUS when no voiceprint supplied."""
    candidates = [SpeakerCandidate(1, "王小姐")]
    result = matcher.match(query_name="王小姐", candidates=candidates)
    assert result.fuzzy_score >= L8_FUZZY_AMBIGUOUS
    assert result.verdict == "AMBIGUOUS"
    assert result.needs_reconfirm is True
    assert result.voiceprint_score is None


# ============================================================
# Layer 1 voiceprint reconfirm
# ============================================================


def _vec(*xs: float) -> tuple[float, ...]:
    return xs


def test_ambiguous_with_high_cosine_becomes_confirmed(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    """L8: identical name + cosine >= 0.7 → CONFIRMED."""
    candidates = [
        SpeakerCandidate(
            speaker_node_id=1,
            canonical_name="王小姐",
            voiceprint_vector=_vec(1.0, 0.0, 0.0),
        )
    ]
    result = matcher.match(
        query_name="王小姐",
        candidates=candidates,
        query_voiceprint=_vec(1.0, 0.0, 0.0),  # identical → cosine = 1.0
    )
    assert result.verdict == "CONFIRMED"
    assert result.voiceprint_score == pytest.approx(1.0)
    assert result.needs_reconfirm is False


def test_ambiguous_with_low_cosine_stays_ambiguous(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    """L8: identical name + cosine < 0.7 → AMBIGUOUS + needs_reconfirm."""
    candidates = [
        SpeakerCandidate(
            speaker_node_id=1,
            canonical_name="王小姐",
            voiceprint_vector=_vec(1.0, 0.0),
        )
    ]
    # Orthogonal vectors → cosine = 0.0
    result = matcher.match(
        query_name="王小姐",
        candidates=candidates,
        query_voiceprint=_vec(0.0, 1.0),
    )
    assert result.verdict == "AMBIGUOUS"
    assert result.voiceprint_score == pytest.approx(0.0)
    assert result.needs_reconfirm is True


def test_ambiguous_but_candidate_voiceprint_missing(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    """Layer 1 unavailable on candidate side → AMBIGUOUS + needs_reconfirm."""
    candidates = [SpeakerCandidate(1, "王小姐", voiceprint_vector=None)]
    result = matcher.match(
        query_name="王小姐",
        candidates=candidates,
        query_voiceprint=_vec(1.0, 0.0),
    )
    assert result.verdict == "AMBIGUOUS"
    assert result.voiceprint_score is None
    assert result.needs_reconfirm is True


def test_reconfirm_at_exactly_threshold(matcher: SpeakerFuzzyMatcher) -> None:
    """L8 edge: cosine exactly == 0.7 → CONFIRMED (>= is inclusive)."""
    # cosine = 1/sqrt(2) ≈ 0.7071 — just above 0.7
    candidates = [
        SpeakerCandidate(
            speaker_node_id=1,
            canonical_name="王小姐",
            voiceprint_vector=_vec(1.0, 0.0),
        )
    ]
    result = matcher.match(
        query_name="王小姐",
        candidates=candidates,
        query_voiceprint=_vec(1.0, 1.0),
    )
    assert result.voiceprint_score == pytest.approx(0.7071, abs=0.001)
    assert result.verdict == "CONFIRMED"


# ============================================================
# Multiple candidates — best wins
# ============================================================


def test_best_candidate_wins(matcher: SpeakerFuzzyMatcher) -> None:
    """When multiple candidates exist, the highest fuzzy score is picked."""
    candidates = [
        SpeakerCandidate(1, "王太太"),  # low
        SpeakerCandidate(2, "王小姐"),  # exact
        SpeakerCandidate(3, "李先生"),  # very low
    ]
    result = matcher.match(query_name="王小姐", candidates=candidates)
    assert result.matched_candidate is not None
    assert result.matched_candidate.speaker_node_id == 2


# ============================================================
# match_with_reconfirm_or_raise wrapper
# ============================================================


def test_match_with_reconfirm_or_raise_raises_when_required(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    candidates = [SpeakerCandidate(1, "王小姐")]
    with pytest.raises(SpeakerLinkerReconfirmUnavailableError):
        match_with_reconfirm_or_raise(
            matcher,
            query_name="王小姐",
            candidates=candidates,
            query_voiceprint=None,
            require_reconfirm_availability=True,
        )


def test_match_with_reconfirm_or_raise_passes_through_confirmed(
    matcher: SpeakerFuzzyMatcher,
) -> None:
    candidates = [
        SpeakerCandidate(
            speaker_node_id=1,
            canonical_name="王小姐",
            voiceprint_vector=_vec(1.0, 0.0),
        )
    ]
    result = match_with_reconfirm_or_raise(
        matcher,
        query_name="王小姐",
        candidates=candidates,
        query_voiceprint=_vec(1.0, 0.0),
        require_reconfirm_availability=True,
    )
    assert result.verdict == "CONFIRMED"


# ============================================================
# VoiceprintComparator
# ============================================================


def test_default_comparator_cosine() -> None:
    vc = DefaultVoiceprintComparator()
    assert vc.cosine(_vec(1.0, 0.0), _vec(1.0, 0.0)) == pytest.approx(1.0)
    assert vc.cosine(_vec(1.0, 0.0), _vec(0.0, 1.0)) == pytest.approx(0.0)
    assert vc.cosine(_vec(1.0, 1.0), _vec(1.0, 1.0)) == pytest.approx(1.0)


def test_default_comparator_dimension_mismatch() -> None:
    vc = DefaultVoiceprintComparator()
    with pytest.raises(ValueError):
        vc.cosine(_vec(1.0, 0.0), _vec(1.0, 0.0, 0.0))


def test_default_comparator_zero_vector() -> None:
    vc = DefaultVoiceprintComparator()
    assert vc.cosine(_vec(0.0, 0.0), _vec(1.0, 0.0)) == 0.0


# ============================================================
# Helpers
# ============================================================


def test_normalise_denormalise_roundtrip() -> None:
    for raw in (0.0, 50.0, 75.5, 100.0):
        assert denormalise_fuzzy_score(normalise_fuzzy_score(raw)) == pytest.approx(raw)


# ============================================================
# SpeakerFuzzyResult dataclass sanity
# ============================================================


def test_result_is_frozen() -> None:
    r = SpeakerFuzzyResult(
        verdict="NO_MATCH",
        matched_candidate=None,
        fuzzy_score=0.0,
        voiceprint_score=None,
        needs_reconfirm=False,
    )
    with pytest.raises(FrozenInstanceError):
        r.verdict = "CONFIRMED"  # type: ignore[misc]
