"""Tests for ``audio_graphy.eval.runner`` — EvalRunner + MockPipeline.

Cases (per arch §7.3.6):
- MockPipeline(precision=1.0) + 3 examples → all aggregate metrics 1.0
- pipeline raises → ex.error non-empty, run continues
- judge=None → 3 LLM-backed metrics skipped (details.skipped=True)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.eval.runner import EvalRunner, MockPipeline
from audio_graphy.eval.types import GoldExample, PredictedResult


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def gold_yaml_3(tmp_path: Path) -> Path:
    """Write a 3-example gold set to tmp_path."""
    content = """
- query: "Q1"
  gold_answer: "A1"
  gold_context_ids: ["c1", "c2"]
  gold_entities: [["E1", "T1"]]
  gold_edges: [["s1", "r1", "d1", "EXTRACTED"]]
  gold_tags: [{tag_path: "p1", value: "v1"}]
- query: "Q2"
  gold_answer: "A2"
  gold_context_ids: ["c3"]
  gold_entities: []
  gold_edges: []
  gold_tags: [{tag_path: "p2", value: "v2"}]
- query: "Q3"
  gold_answer: "A3"
  gold_context_ids: ["c4"]
  gold_entities: [["X", "Y"]]
  gold_edges: []
  gold_tags: []
""".strip()
    p = tmp_path / "smoke.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class _FailingPipeline:
    """Pipeline that always raises — for error-tolerance testing."""

    async def predict(self, gold: GoldExample) -> PredictedResult:
        raise RuntimeError("boom")


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_runner_mock_pipeline_3_examples_perfect(gold_yaml_3: Path) -> None:
    """MockPipeline(precision=1.0) → non-LLM aggregate metrics all 1.0."""
    runner = EvalRunner(
        gold_set_path=gold_yaml_3,
        pipeline=MockPipeline(precision=1.0),
        judge=None,
    )
    run = await runner.run()
    assert len(run.per_example) == 3
    assert all(ex.error is None for ex in run.per_example)
    # Non-LLM metrics should all be 1.0 for the perfect mock where the example
    # has non-empty gold; tag_accuracy on example 3 is 0.0 (gold_tags empty →
    # denominator_zero), so aggregate is the mean across all three.
    for name in (
        "context_precision_at_5",
        "context_recall",
        "entity_f1",
    ):
        assert run.aggregate_metrics[name] == pytest.approx(1.0), name
    # LLM metrics skipped → not included in aggregate (empty values list).
    for name in ("faithfulness", "answer_relevance", "factual_correctness"):
        # Aggregate may still include them if all examples returned 0.0 — check
        # the per-example flag instead.
        for ex in run.per_example:
            m = next((m for m in ex.metrics if m.name == name), None)
            assert m is not None
            assert m.details.get("skipped") is True


@pytest.mark.asyncio
async def test_runner_pipeline_error_tolerated(gold_yaml_3: Path) -> None:
    """Pipeline raises → ex.error non-empty, run does not crash."""
    runner = EvalRunner(
        gold_set_path=gold_yaml_3,
        pipeline=_FailingPipeline(),
        judge=None,
    )
    run = await runner.run()
    assert len(run.per_example) == 3
    for ex in run.per_example:
        assert ex.error is not None
        assert "boom" in ex.error
        assert ex.metrics == ()
    # Aggregate is empty because all examples errored.
    assert run.aggregate_metrics == {}


@pytest.mark.asyncio
async def test_runner_aggregate_skips_errors(tmp_path: Path) -> None:
    """Errored examples are excluded from aggregate; partial mean uses ok ones."""
    yaml_path = tmp_path / "mix.yaml"
    yaml_path.write_text(
        '- query: "q1"\n  gold_answer: "a1"\n  gold_context_ids: ["c1"]\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n'
        '- query: "q2"\n  gold_answer: "a2"\n  gold_context_ids: ["c2"]\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n',
        encoding="utf-8",
    )

    class _FlakyPipeline:
        async def predict(self, gold: GoldExample) -> PredictedResult:
            if gold.query == "q1":
                raise RuntimeError("q1 fails")
            return PredictedResult(
                query=gold.query,
                answer=gold.gold_answer,
                retrieved_context_ids=gold.gold_context_ids,
                entities=gold.gold_entities,
                edges=gold.gold_edges,
                tags=gold.gold_tags,
            )

    runner = EvalRunner(
        gold_set_path=yaml_path,
        pipeline=_FlakyPipeline(),
        judge=None,
    )
    run = await runner.run()
    assert len(run.per_example) == 2
    assert run.per_example[0].error is not None  # q1 crashed
    assert run.per_example[1].error is None
    # Aggregate should be computed over the single successful example → all 1.0.
    assert run.aggregate_metrics["context_recall"] == pytest.approx(1.0)
    assert run.aggregate_metrics["tag_accuracy"] == pytest.approx(0.0)  # gold_tags empty


@pytest.mark.asyncio
async def test_runner_judge_none_marks_skipped(gold_yaml_3: Path) -> None:
    """judge=None → all 3 LLM metrics recorded with details.skipped=True."""
    runner = EvalRunner(
        gold_set_path=gold_yaml_3,
        pipeline=MockPipeline(),
        judge=None,
    )
    run = await runner.run()
    skipped_names = {"faithfulness", "answer_relevance", "factual_correctness"}
    for ex in run.per_example:
        for name in skipped_names:
            m = next((mm for mm in ex.metrics if mm.name == name), None)
            assert m is not None, f"missing {name} in {ex.example_id}"
            assert m.value == 0.0
            assert m.details.get("skipped") is True


@pytest.mark.asyncio
async def test_runner_missing_gold_file_raises(tmp_path: Path) -> None:
    """Missing gold set file → FileNotFoundError."""
    runner = EvalRunner(
        gold_set_path=tmp_path / "nonexistent.yaml",
        pipeline=MockPipeline(),
        judge=None,
    )
    with pytest.raises(FileNotFoundError):
        await runner.run()


def test_mock_pipeline_repr() -> None:
    """MockPipeline.__repr__ includes precision."""
    p = MockPipeline(precision=0.7)
    assert "0.7" in repr(p)
