"""Tests for ``EvalRunner`` position de-bias logic (M6 WS-2).

Verifies:
- With ``position_debias=True``: each LLM-judge metric calls the judge
  twice (original + reversed retrieved_text) and reports the mean.
- With ``position_debias=False``: each LLM-judge metric calls the judge
  exactly once.
- Mean result equals ``(original + reversed) / 2``.

A counting stub ``_CountingJudge`` records how many times each judge
method is invoked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from audio_graphy.eval.runner import EvalRunner, MockPipeline
from audio_graphy.eval.types import GoldExample, MetricResult, PredictedResult


class _CountingJudge:
    """Stub judge that records every call and returns a deterministic score.

    ``judge_relevance`` returns 1.0 on the first call (original order) and
    0.0 on the second call (reversed order). This lets the test assert
    that de-bias computes the mean = 0.5.
    """

    def __init__(self) -> None:
        self.relevance_calls: list[tuple[str, str]] = []
        self.facts_calls: list[str] = []
        self.faith_calls: list[tuple[str, list[str]]] = []

    async def extract_facts(self, text: str) -> list[str]:
        self.facts_calls.append(text)
        # Return one fake fact per call so faithfulness has something to score.
        return ["fact-1"]

    async def judge_faithfulness(
        self, context: str, facts: list[str]
    ) -> list[bool]:
        self.faith_calls.append((context, list(facts)))
        # Alternate True / False to make the mean-check meaningful.
        return [len(self.faith_calls) % 2 == 1]

    async def judge_relevance(self, query: str, answer: str) -> float:
        self.relevance_calls.append((query, answer))
        # First call → 1.0, second call → 0.0, repeating.
        return [1.0, 0.0][(len(self.relevance_calls) - 1) % 2]


@pytest.fixture
def gold_yaml(tmp_path: Path) -> Path:
    content = """
- query: "Q1"
  gold_answer: "A1"
  gold_context_ids: ["c1"]
  gold_entities: []
  gold_edges: []
  gold_tags: []
""".strip()
    p = tmp_path / "smoke.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _make_pred_with_retrieved_text() -> Any:
    """Build a pipeline that returns a PredictedResult with a retrieved_text tag."""
    class _Pipeline:
        async def predict(self, gold: GoldExample) -> PredictedResult:
            return PredictedResult(
                query=gold.query,
                answer="answer text",
                retrieved_context_ids=("c1",),
                entities=(),
                edges=(),
                tags=(
                    {"tag_path": "retrieved_text", "value": "line1\nline2\nline3"},
                ),
            )
    return _Pipeline()


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_position_debias_runs_judge_twice(gold_yaml: Path) -> None:
    """With de-bias on, judge_relevance is called twice per example."""
    judge = _CountingJudge()
    runner = EvalRunner(
        gold_set_path=gold_yaml,
        pipeline=_make_pred_with_retrieved_text(),
        judge=judge,
        position_debias=True,
    )
    run = await runner.run()
    assert len(run.per_example) == 1
    assert run.per_example[0].error is None
    # judge_relevance should be called twice (original + reversed).
    assert len(judge.relevance_calls) == 2


@pytest.mark.asyncio
async def test_position_debias_disabled_runs_judge_once(gold_yaml: Path) -> None:
    """With de-bias off, judge_relevance is called once per example."""
    judge = _CountingJudge()
    runner = EvalRunner(
        gold_set_path=gold_yaml,
        pipeline=_make_pred_with_retrieved_text(),
        judge=judge,
        position_debias=False,
    )
    run = await runner.run()
    assert len(run.per_example) == 1
    assert run.per_example[0].error is None
    # judge_relevance should be called once (original only).
    assert len(judge.relevance_calls) == 1


@pytest.mark.asyncio
async def test_position_debias_mean_is_correct(gold_yaml: Path) -> None:
    """De-biased answer_relevance = mean(1.0, 0.0) = 0.5."""
    judge = _CountingJudge()
    runner = EvalRunner(
        gold_set_path=gold_yaml,
        pipeline=_make_pred_with_retrieved_text(),
        judge=judge,
        position_debias=True,
    )
    run = await runner.run()
    metrics = {m.name: m for ex in run.per_example for m in ex.metrics}
    relevance: MetricResult = metrics["answer_relevance"]
    # Mean of 1.0 and 0.0.
    assert relevance.value == pytest.approx(0.5)
    # Details should record both halves + the debiased flag.
    assert relevance.details.get("debiased") is True
    assert relevance.details.get("value_original") == 1.0
    assert relevance.details.get("value_reversed") == 0.0
