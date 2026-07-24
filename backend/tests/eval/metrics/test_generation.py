"""Unit tests for generation metrics — 12 cases.

Cases:
- faithfulness: perfect / zero / empty_answer / no_facts
- answer_relevance: perfect / half / zero / empty_answer
- factual_correctness: perfect / both_empty / no_overlap / partial
"""

from __future__ import annotations

import pytest

from audio_graphy.eval.metrics.generation import (
    answer_relevance,
    factual_correctness,
    faithfulness,
)
from audio_graphy.eval.types import GoldExample, PredictedResult
from tests.eval.metrics.conftest import make_stub


def _gold(answer: str = "gold answer") -> GoldExample:
    return GoldExample(
        query="q",
        gold_answer=answer,
        gold_context_ids=(),
        gold_entities=(),
        gold_edges=(),
        gold_tags=(),
    )


def _pred(
    answer: str = "pred answer",
    tags: tuple[dict[str, str], ...] = ({"tag_path": "retrieved_text", "value": "ctx"},),
) -> PredictedResult:
    return PredictedResult(
        query="q",
        answer=answer,
        retrieved_context_ids=("c1",),
        entities=(),
        edges=(),
        tags=tags,
    )


# -------- faithfulness --------


async def test_faithfulness_perfect() -> None:
    """All facts supported → 1.0."""
    gold = _gold()
    pred = _pred()
    judge = make_stub(facts=["f1", "f2"], flags=[True, True])
    m = await faithfulness(gold, pred, judge)
    assert m.value == pytest.approx(1.0)
    assert m.details["supported_count"] == 2


async def test_faithfulness_zero() -> None:
    """No facts supported → 0.0."""
    gold = _gold()
    pred = _pred()
    judge = make_stub(facts=["f1", "f2"], flags=[False, False])
    m = await faithfulness(gold, pred, judge)
    assert m.value == pytest.approx(0.0)


async def test_faithfulness_empty_answer() -> None:
    """Empty pred.answer → reason=empty_answer, value 0.0, judge not called."""
    gold = _gold()
    pred = _pred(answer="")
    judge = make_stub(facts=["f1"])  # should not be touched
    m = await faithfulness(gold, pred, judge)
    assert m.value == 0.0
    assert m.details["reason"] == "empty_answer"


async def test_faithfulness_empty_context() -> None:
    """Missing retrieved_text tag → reason=empty_context."""
    gold = _gold()
    pred = _pred(tags=())  # no retrieved_text tag
    judge = make_stub(facts=["f1"])
    m = await faithfulness(gold, pred, judge)
    assert m.value == 0.0
    assert m.details["reason"] == "empty_context"


async def test_faithfulness_no_facts() -> None:
    """Judge returns [] from extract_facts → reason=no_facts_extracted."""
    gold = _gold()
    pred = _pred()
    judge = make_stub(facts=[], flags=[True, True])
    m = await faithfulness(gold, pred, judge)
    assert m.value == 0.0
    assert m.details["reason"] == "no_facts_extracted"


# -------- answer_relevance --------


async def test_answer_relevance_perfect() -> None:
    gold = _gold()
    pred = _pred()
    judge = make_stub(score=1.0)
    m = await answer_relevance(gold, pred, judge)
    assert m.value == pytest.approx(1.0)


async def test_answer_relevance_half() -> None:
    gold = _gold()
    pred = _pred()
    judge = make_stub(score=0.5)
    m = await answer_relevance(gold, pred, judge)
    assert m.value == pytest.approx(0.5)


async def test_answer_relevance_zero() -> None:
    gold = _gold()
    pred = _pred()
    judge = make_stub(score=0.0)
    m = await answer_relevance(gold, pred, judge)
    assert m.value == pytest.approx(0.0)


async def test_answer_relevance_empty_answer() -> None:
    gold = _gold()
    pred = _pred(answer="")
    judge = make_stub(score=1.0)  # judge should not be called
    m = await answer_relevance(gold, pred, judge)
    assert m.value == 0.0
    assert m.details["reason"] == "empty_answer"


# -------- factual_correctness --------


async def test_factual_correctness_perfect() -> None:
    """fact_pred == fact_gold → 1.0."""
    gold = _gold()
    pred = _pred()
    judge = make_stub(facts=["f1", "f2"])
    m = await factual_correctness(gold, pred, judge)
    assert m.value == pytest.approx(1.0)
    assert m.details["tp"] == 2


async def test_factual_correctness_both_empty() -> None:
    """Both fact sets empty → reason=both_empty, value 1.0 (PRD §5.3.3)."""
    gold = _gold()
    pred = _pred()
    judge = make_stub(facts=[])
    m = await factual_correctness(gold, pred, judge)
    assert m.value == pytest.approx(1.0)
    assert m.details["reason"] == "both_empty"


async def test_factual_correctness_no_overlap() -> None:
    """Disjoint sets → F1=0."""

    # Use different fact lists for pred vs gold via two judges — but our stub
    # returns the same list regardless of input. Instead simulate by making
    # the gold answer text extractable only with a different stub config:
    # since stub ignores input, just configure disjoint lists per call.
    class _TwoMode:
        """Stub that alternates fact list based on call index."""

        def __init__(self) -> None:
            self.calls = 0

        async def extract_facts(self, text: str) -> list[str]:
            self.calls += 1
            # First call = pred, second = gold.
            return ["p1", "p2"] if self.calls == 1 else ["g1", "g2"]

        async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
            return [True] * len(facts)

        async def judge_relevance(self, query: str, answer: str) -> float:
            return 1.0

    gold = _gold()
    pred = _pred()
    m = await factual_correctness(gold, pred, _TwoMode())
    assert m.value == pytest.approx(0.0)
    assert m.details["tp"] == 0


async def test_factual_correctness_partial() -> None:
    """1 of 2 overlap → precision=recall=0.5 → F1=0.5."""

    class _Overlapping:
        """Stub returning different fact lists per call index."""

        def __init__(self) -> None:
            self.calls = 0

        async def extract_facts(self, text: str) -> list[str]:
            self.calls += 1
            # First call = pred, second = gold; share "shared" fact.
            return ["shared", "p1"] if self.calls == 1 else ["shared", "g1"]

        async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
            return [True] * len(facts)

        async def judge_relevance(self, query: str, answer: str) -> float:
            return 1.0

    gold = _gold()
    pred = _pred()
    m = await factual_correctness(gold, pred, _Overlapping())
    assert m.value == pytest.approx(0.5)
    assert m.details["tp"] == 1
