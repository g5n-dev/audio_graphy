"""Extra runner tests — gap-fill coverage for eval/runner.py (84% → ≥90%).

Targeted uncovered branches (per QA verification 2026-07-21):
- MockPipeline precision<1.0 branch (L82-89)
- _compute_metrics failure path (L169-171)
- judge-enabled metric path (L198-205)
- gold set parse edge cases (L230, L238, L262-263)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.eval.runner import EvalRunner, MockPipeline
from audio_graphy.eval.types import GoldExample, PredictedResult
from tests.eval.metrics.conftest import make_stub


@pytest.mark.asyncio
async def test_mock_pipeline_precision_zero_returns_empty_pred(
    tmp_path: Path,
) -> None:
    """MockPipeline(precision=0.0) → empty PredictedResult path (L82-89)."""
    yaml_path = tmp_path / "tiny.yaml"
    yaml_path.write_text(
        '- query: "q"\n  gold_answer: "a"\n  gold_context_ids: []\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n',
        encoding="utf-8",
    )
    runner = EvalRunner(
        gold_set_path=yaml_path,
        pipeline=MockPipeline(precision=0.0),
        judge=None,
    )
    run = await runner.run()
    assert len(run.per_example) == 1
    ex = run.per_example[0]
    assert ex.error is None
    # Empty pred + empty gold → entity_f1 both_empty → 1.0 (PRD §5.3.3).
    entity_metric = next(m for m in ex.metrics if m.name == "entity_f1")
    assert entity_metric.value == pytest.approx(1.0)
    assert entity_metric.details["reason"] == "both_empty"
    # All other non-LLM metrics → 0.0 (denominator_zero or empty pred).
    prec = next(m for m in ex.metrics if m.name == "context_precision_at_5")
    assert prec.value == 0.0


@pytest.mark.asyncio
async def test_runner_metric_failure_captured_in_error(tmp_path: Path) -> None:
    """Metric raising → captured in EvalExampleResult.error (L169-171).

    We inject a broken judge (extract_facts raises) and confirm the runner
    records the failure rather than crashing the whole run. Note: the judge's
    extract_facts() is invoked from inside faithfulness/factual_correctness;
    a non-empty pred context would normally be required to reach the judge,
    so we use a custom pipeline that injects a retrieved_text tag.
    """
    yaml_path = tmp_path / "two.yaml"
    yaml_path.write_text(
        '- query: "q1"\n  gold_answer: "a1"\n  gold_context_ids: ["c1"]\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n'
        '- query: "q2"\n  gold_answer: "a2"\n  gold_context_ids: ["c2"]\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n',
        encoding="utf-8",
    )

    class _PipelineWithTag:
        """MockPipeline-like, but injects a retrieved_text tag so the judge runs."""

        async def predict(self, gold: GoldExample) -> PredictedResult:
            return PredictedResult(
                query=gold.query,
                answer=gold.gold_answer,
                retrieved_context_ids=gold.gold_context_ids,
                entities=gold.gold_entities,
                edges=gold.gold_edges,
                tags=(*gold.gold_tags, {"tag_path": "retrieved_text", "value": "ctx"}),
            )

    class _BoomJudge:
        """Judge whose extract_facts always raises (forces metric failure)."""

        async def extract_facts(self, text: str) -> list[str]:
            raise RuntimeError("judge exploded")

        async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
            return [True] * len(facts)

        async def judge_relevance(self, query: str, answer: str) -> float:
            return 1.0

    runner = EvalRunner(
        gold_set_path=yaml_path,
        pipeline=_PipelineWithTag(),
        judge=_BoomJudge(),  # type: ignore[arg-type]
    )
    run = await runner.run()
    assert len(run.per_example) == 2
    # Both examples should have errored because extract_facts raised.
    for ex in run.per_example:
        assert ex.error is not None
        assert "judge exploded" in ex.error
        assert ex.metrics == ()


@pytest.mark.asyncio
async def test_runner_judge_enabled_runs_llm_metrics(tmp_path: Path) -> None:
    """judge != None → 3 LLM metrics actually computed (L198-205)."""
    yaml_path = tmp_path / "tiny.yaml"
    yaml_path.write_text(
        '- query: "q"\n  gold_answer: "gold answer"\n'
        '  gold_context_ids: ["c1"]\n'
        '  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n',
        encoding="utf-8",
    )

    class _PipelineWithTag:
        """MockPipeline-like, but injects retrieved_text so faithfulness runs."""

        async def predict(self, gold: GoldExample) -> PredictedResult:
            return PredictedResult(
                query=gold.query,
                answer=gold.gold_answer,
                retrieved_context_ids=gold.gold_context_ids,
                entities=gold.gold_entities,
                edges=gold.gold_edges,
                tags=(*gold.gold_tags, {"tag_path": "retrieved_text", "value": "ctx"}),
            )

    class _GoodJudge:
        async def extract_facts(self, text: str) -> list[str]:
            return ["one-fact"]

        async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
            return [True] * len(facts)

        async def judge_relevance(self, query: str, answer: str) -> float:
            return 1.0

    runner = EvalRunner(
        gold_set_path=yaml_path,
        pipeline=_PipelineWithTag(),
        judge=_GoodJudge(),  # type: ignore[arg-type]
    )
    run = await runner.run()
    assert len(run.per_example) == 1
    ex = run.per_example[0]
    assert ex.error is None

    # LLM metrics computed (not skipped).
    faith = next(m for m in ex.metrics if m.name == "faithfulness")
    assert faith.value == pytest.approx(1.0)
    assert faith.details.get("skipped") is not True
    rel = next(m for m in ex.metrics if m.name == "answer_relevance")
    assert rel.value == pytest.approx(1.0)
    fc = next(m for m in ex.metrics if m.name == "factual_correctness")
    # pred answer == gold_answer via the echo pipeline → same fact set → F1=1.0.
    assert fc.value == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_runner_gold_set_not_yaml_list_raises(tmp_path: Path) -> None:
    """Gold set YAML that parses to a dict (not list) → ValueError (L230)."""
    bad = tmp_path / "dict.yaml"
    bad.write_text("query: q1\ngold_answer: a1\n", encoding="utf-8")
    runner = EvalRunner(
        gold_set_path=bad,
        pipeline=MockPipeline(),
        judge=None,
    )
    with pytest.raises(ValueError, match="YAML list"):
        await runner.run()


@pytest.mark.asyncio
async def test_runner_gold_set_item_not_mapping_raises(tmp_path: Path) -> None:
    """Gold set item is a scalar (not dict) → ValueError (L238)."""
    bad = tmp_path / "scalar.yaml"
    bad.write_text("- just-a-string\n- another\n", encoding="utf-8")
    runner = EvalRunner(
        gold_set_path=bad,
        pipeline=MockPipeline(),
        judge=None,
    )
    with pytest.raises(ValueError, match="not a mapping"):
        await runner.run()


@pytest.mark.asyncio
async def test_runner_gold_set_item_missing_query_raises(tmp_path: Path) -> None:
    """Gold set item missing required `query` key → ValueError (L262-263)."""
    bad = tmp_path / "missing.yaml"
    bad.write_text(
        "- gold_answer: 'a'\n  gold_context_ids: []\n"
        "  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n",
        encoding="utf-8",
    )
    runner = EvalRunner(
        gold_set_path=bad,
        pipeline=MockPipeline(),
        judge=None,
    )
    with pytest.raises(ValueError, match="missing required key"):
        await runner.run()
