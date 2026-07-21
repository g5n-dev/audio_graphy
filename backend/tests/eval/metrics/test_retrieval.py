"""Unit tests for retrieval metrics — 8 cases.

Cases:
- context_precision perfect / partial / k=0 / empty_gold
- context_recall    perfect / partial / empty_gold / order_irrelevant
"""

from __future__ import annotations

import pytest

from audio_graphy.eval.metrics.retrieval import (
    context_precision_at_k,
    context_recall,
)
from audio_graphy.eval.types import GoldExample, PredictedResult


def _gold(context_ids: tuple[str, ...]) -> GoldExample:
    return GoldExample(
        query="q",
        gold_answer="a",
        gold_context_ids=context_ids,
        gold_entities=(),
        gold_edges=(),
        gold_tags=(),
    )


def _pred(retrieved: tuple[str, ...], answer: str = "answer") -> PredictedResult:
    return PredictedResult(
        query="q",
        answer=answer,
        retrieved_context_ids=retrieved,
        entities=(),
        edges=(),
        tags=(),
    )


# -------- context_precision_at_k --------


def test_context_precision_perfect() -> None:
    """gold ⊆ retrieved[:k] → 1.0."""
    gold = _gold(("c1", "c2", "c3"))
    pred = _pred(("c1", "c2", "c3", "c4", "c5"))
    m = context_precision_at_k(gold, pred, k=5)
    assert m.value == pytest.approx(1.0)
    assert m.details["hits"] == 3
    assert m.details["k"] == 5


def test_context_precision_partial() -> None:
    """2 of 5 retrieved hit gold, denom=min(5,3)=3 → 2/3."""
    gold = _gold(("c1", "c2", "c3"))
    pred = _pred(("c1", "c2", "x1", "x2", "x3"))
    m = context_precision_at_k(gold, pred, k=5)
    assert m.value == pytest.approx(2 / 3)
    assert m.details["hits"] == 2


def test_context_precision_k_zero() -> None:
    """k=0 → denominator_zero, value 0.0."""
    gold = _gold(("c1",))
    pred = _pred(("c1",))
    m = context_precision_at_k(gold, pred, k=0)
    assert m.value == 0.0
    assert m.details["denominator_zero"] is True
    assert m.details["k"] == 0


def test_context_precision_empty_gold() -> None:
    """Empty gold_context_ids → denominator_zero, value 0.0."""
    gold = _gold(())
    pred = _pred(("c1", "c2"))
    m = context_precision_at_k(gold, pred, k=5)
    assert m.value == 0.0
    assert m.details["denominator_zero"] is True


# -------- context_recall --------


def test_context_recall_perfect() -> None:
    """All gold chunks retrieved → 1.0."""
    gold = _gold(("c1", "c2", "c3"))
    pred = _pred(("c1", "c2", "c3", "c4"))
    m = context_recall(gold, pred)
    assert m.value == pytest.approx(1.0)
    assert m.details["hits"] == 3


def test_context_recall_partial() -> None:
    """1 of 3 gold retrieved → 1/3."""
    gold = _gold(("c1", "c2", "c3"))
    pred = _pred(("c1", "x1", "x2"))
    m = context_recall(gold, pred)
    assert m.value == pytest.approx(1 / 3)


def test_context_recall_empty_gold() -> None:
    """Empty gold → denominator_zero, value 0.0."""
    gold = _gold(())
    pred = _pred(("c1", "c2"))
    m = context_recall(gold, pred)
    assert m.value == 0.0
    assert m.details["denominator_zero"] is True


def test_context_recall_order_irrelevant() -> None:
    """Recall ignores rank order — set semantics."""
    gold = _gold(("c1", "c2"))
    pred = _pred(("c2", "c1"))  # reversed
    m = context_recall(gold, pred)
    assert m.value == pytest.approx(1.0)
