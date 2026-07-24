"""Unit tests for entity_f1 dual-mode (strict + fuzzy) — 4 cases.

Cases cover docs/m6-prd.md §6.5 acceptance:
    1. Both strict and fuzzy returned with distinct names.
    2. Partial fuzzy match scores correctly (CS75 Plus ↔ CS75PLUS).
    3. fuzzy_threshold=1.0 behaves identically to strict mode.
    4. Both empty → both return 1.0.
"""

from __future__ import annotations

import pytest

from audio_graphy.eval.metrics.audio_graphy import entity_f1
from audio_graphy.eval.types import GoldExample, PredictedResult


def _gold(entities: tuple[tuple[str, str], ...]) -> GoldExample:
    return GoldExample(
        query="q",
        gold_answer="a",
        gold_context_ids=(),
        gold_entities=entities,
        gold_edges=(),
        gold_tags=(),
    )


def _pred(entities: tuple[tuple[str, str], ...]) -> PredictedResult:
    return PredictedResult(
        query="q",
        answer="a",
        retrieved_context_ids=(),
        entities=entities,
        edges=(),
        tags=(),
    )


# --------------------------------------------------------------------
# Case 1 — strict + fuzzy both returned with distinct names
# --------------------------------------------------------------------


def test_dual_mode_returns_two_metrics() -> None:
    """Calling entity_f1 with strict + fuzzy yields two MetricResults with distinct names."""
    gold = _gold((("CS75 Plus", "车型"),))
    pred = _pred((("CS75 Plus", "车型"),))
    strict = entity_f1(gold, pred, fuzzy_threshold=1.0)
    fuzzy = entity_f1(gold, pred, fuzzy_threshold=0.85)
    assert strict.name == "entity_f1"
    assert fuzzy.name == "entity_f1_fuzzy"
    assert strict.value == pytest.approx(1.0)
    assert fuzzy.value == pytest.approx(1.0)


# --------------------------------------------------------------------
# Case 2 — partial fuzzy match scores correctly
# --------------------------------------------------------------------


def test_partial_fuzzy_match() -> None:
    """CS75 Plus (gold) vs CS75PLUS (pred): strict fails, fuzzy passes.

    With one gold and one pred, fuzzy match = TP=1 → P=R=F1=1.0.
    Strict mode: TP=0 → F1=0.0.
    """
    gold = _gold((("CS75 Plus", "车型"),))
    pred = _pred((("CS75PLUS", "车型"),))
    strict = entity_f1(gold, pred, fuzzy_threshold=1.0)
    fuzzy = entity_f1(gold, pred, fuzzy_threshold=0.85)
    assert strict.value == pytest.approx(0.0)
    assert fuzzy.value == pytest.approx(1.0)
    assert fuzzy.details["tp"] == 1
    assert strict.details["tp"] == 0


# --------------------------------------------------------------------
# Case 3 — fuzzy_threshold=1.0 behaves identically to strict mode
# --------------------------------------------------------------------


def test_threshold_1_equals_strict() -> None:
    """threshold=1.0 → fuzzy mode is disabled, behaves exactly like strict."""
    gold = _gold((("CS75 Plus", "车型"), ("哈弗H6", "竞品")))
    pred = _pred((("CS75 Plus", "车型"), ("UNI-V", "竞品")))
    strict = entity_f1(gold, pred, fuzzy_threshold=1.0)
    # No fuzzy match possible at threshold=1.0; identical name + type check.
    assert strict.value < 1.0  # only 1 of 2 matched
    assert strict.details["tp"] == 1
    # Name should be "entity_f1" (not "entity_f1_fuzzy").
    assert strict.name == "entity_f1"


# --------------------------------------------------------------------
# Case 4 — both empty → both return 1.0
# --------------------------------------------------------------------


def test_both_empty_returns_one() -> None:
    """Both gold and pred entity sets empty → strict and fuzzy both 1.0."""
    gold = _gold(())
    pred = _pred(())
    strict = entity_f1(gold, pred, fuzzy_threshold=1.0)
    fuzzy = entity_f1(gold, pred, fuzzy_threshold=0.85)
    assert strict.value == pytest.approx(1.0)
    assert fuzzy.value == pytest.approx(1.0)
    assert strict.details["reason"] == "both_empty"
    assert fuzzy.details["reason"] == "both_empty"


# --------------------------------------------------------------------
# Bonus — entity type mismatch blocks fuzzy match
# --------------------------------------------------------------------


def test_fuzzy_type_mismatch_blocks_match() -> None:
    """Same text but different type → fuzzy does NOT match."""
    gold = _gold((("CS75 Plus", "车型"),))
    pred = _pred((("CS75PLUS", "竞品"),))  # type differs
    fuzzy = entity_f1(gold, pred, fuzzy_threshold=0.85)
    assert fuzzy.value == pytest.approx(0.0)
