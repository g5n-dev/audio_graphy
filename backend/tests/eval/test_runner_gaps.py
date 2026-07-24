"""Coverage gap-fill tests for EvalRunner + RAGPipeline.

Targets the uncovered branches in eval/runner.py:
- _eval_one: pipeline crash → captured as EvalExampleResult(error=...)
- _eval_one: metric computation crash → captured as EvalExampleResult(error=...)
- _load_gold_set: file missing → FileNotFoundError
- _load_gold_set: YAML not a list → ValueError
- _load_gold_set: item not a mapping → ValueError
- _load_gold_set: missing 'query' key → ValueError
- EvalRunner with explicit position_debias=False → no debias
- EvalRunner with settings provided → reads eval_position_debias
- EvalRunner with explicit entity_fuzzy_threshold
- _reverse_retrieved_text with no retrieved_text tag → no-op
- _aggregate skips errored examples
- RAGPipeline when query_service is None and graph_store is None → builds them
- RAGPipeline when query_service is None and vector_store is None → RuntimeError
- RAGPipeline predict with empty answer text → empty entities
- _extract_answer_entities: exception path returns empty
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from audio_graphy.eval.runner import EvalRunner, MockPipeline, RAGPipeline
from audio_graphy.eval.types import GoldExample, PredictedResult


def _write_gold(path: Path, items: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump(items), encoding="utf-8")
    return path


def _gold_item(query: str = "Q1", answer: str = "A1") -> dict[str, Any]:
    return {
        "query": query,
        "gold_answer": answer,
        "gold_context_ids": ["c1"],
        "gold_entities": [["CS75 Plus", "车型"]],
        "gold_edges": [],
        "gold_tags": [],
    }


# ============================================================
# EvalRunner — error / branch coverage
# ============================================================


@pytest.mark.asyncio
async def test_pipeline_crash_captured_as_error(tmp_path: Path) -> None:
    """Pipeline.predict raising → EvalExampleResult(error=...), aggregate still computed."""

    class _CrashingPipeline:
        async def predict(self, gold: GoldExample) -> PredictedResult:
            raise RuntimeError("pipeline exploded")

    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])

    runner = EvalRunner(gold_set_path=gold_path, pipeline=_CrashingPipeline())
    run = await runner.run()
    assert run.per_example[0].error is not None
    assert "pipeline exploded" in run.per_example[0].error
    # Aggregate excludes errored examples.
    assert run.aggregate_metrics == {}


@pytest.mark.asyncio
async def test_metric_computation_cratch_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _compute_metrics raises, the example is flagged errored."""

    class _GoodPipeline:
        async def predict(self, gold: GoldExample) -> PredictedResult:
            return PredictedResult(
                query=gold.query,
                answer="x",
                retrieved_context_ids=(),
                entities=(),
                edges=(),
                tags=(),
            )

    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])

    runner = EvalRunner(gold_set_path=gold_path, pipeline=_GoodPipeline())

    async def _boom_metrics(_gold: GoldExample, _pred: PredictedResult) -> list:
        raise RuntimeError("metric crashed")

    monkeypatch.setattr(runner, "_compute_metrics", _boom_metrics)

    run = await runner.run()
    assert run.per_example[0].error is not None
    assert "metric crashed" in run.per_example[0].error


def test_load_gold_set_missing_file_raises(tmp_path: Path) -> None:
    """FileNotFoundError surfaces when the gold set path doesn't exist."""
    runner = EvalRunner(gold_set_path=tmp_path / "absent.yaml", pipeline=MockPipeline())
    with pytest.raises(FileNotFoundError, match="gold set not found"):
        runner._load_gold_set()


def test_load_gold_set_non_list_yaml_raises(tmp_path: Path) -> None:
    """YAML root is not a list → ValueError."""
    path = tmp_path / "dict.yaml"
    path.write_text("foo: bar\n", encoding="utf-8")
    runner = EvalRunner(gold_set_path=path, pipeline=MockPipeline())
    with pytest.raises(ValueError, match="must be a YAML list"):
        runner._load_gold_set()


def test_load_gold_set_item_not_mapping_raises(tmp_path: Path) -> None:
    """A gold item that is not a dict → ValueError."""
    path = tmp_path / "str.yaml"
    path.write_text("- just-a-string\n", encoding="utf-8")
    runner = EvalRunner(gold_set_path=path, pipeline=MockPipeline())
    with pytest.raises(ValueError, match="gold\\[0\\] is not a mapping"):
        runner._load_gold_set()


def test_load_gold_set_missing_required_key_raises(tmp_path: Path) -> None:
    """A gold item missing 'query' → ValueError with index."""
    path = tmp_path / "missing.yaml"
    path.write_text("- gold_answer: A1\n", encoding="utf-8")
    runner = EvalRunner(gold_set_path=path, pipeline=MockPipeline())
    with pytest.raises(ValueError, match="gold\\[0\\] missing required key"):
        runner._load_gold_set()


@pytest.mark.asyncio
async def test_explicit_position_debias_false_disables_debias(tmp_path: Path) -> None:
    """EvalRunner(position_debias=False) skips the debias double-judge."""
    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])
    runner = EvalRunner(gold_set_path=gold_path, pipeline=MockPipeline(), position_debias=False)
    assert runner._position_debias is False
    assert runner._config_snapshot["position_debias"] == "False"


@pytest.mark.asyncio
async def test_settings_provides_eval_position_debias(tmp_path: Path) -> None:
    """When settings is passed without position_debias arg, settings.eval_position_debias wins."""

    class _Settings:
        eval_concurrency = 2
        eval_position_debias = False
        entity_fuzzy_threshold = 0.9

    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])
    runner = EvalRunner(gold_set_path=gold_path, pipeline=MockPipeline(), settings=_Settings())
    assert runner._position_debias is False
    assert runner._entity_fuzzy_threshold == 0.9
    assert runner._config_snapshot["entity_fuzzy_threshold"] == "0.9"


@pytest.mark.asyncio
async def test_explicit_entity_fuzzy_threshold(tmp_path: Path) -> None:
    """Explicit entity_fuzzy_threshold arg overrides settings/default."""
    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])
    runner = EvalRunner(
        gold_set_path=gold_path,
        pipeline=MockPipeline(),
        entity_fuzzy_threshold=0.78,
    )
    assert runner._entity_fuzzy_threshold == 0.78


def test_reverse_retrieved_text_no_tag_returns_input_unchanged() -> None:
    """_reverse_retrieved_text with no retrieved_text tag is a no-op."""
    pred = PredictedResult(
        query="Q1",
        answer="A1",
        retrieved_context_ids=("c1",),
        entities=(),
        edges=(),
        tags=({"tag_path": "other", "value": "v"},),
    )
    out = EvalRunner._reverse_retrieved_text(pred)
    # Same object returned (no rewrite needed).
    assert out is pred


def test_reverse_retrieved_text_with_tag_returns_new_pred() -> None:
    """A pred with retrieved_text tag is reversed (multi-line)."""
    pred = PredictedResult(
        query="Q1",
        answer="A1",
        retrieved_context_ids=("c1",),
        entities=(),
        edges=(),
        tags=(
            {"tag_path": "retrieved_text", "value": "line1\nline2\nline3"},
            {"tag_path": "other", "value": "v"},
        ),
    )
    out = EvalRunner._reverse_retrieved_text(pred)
    assert out is not pred
    rt = next(t for t in out.tags if t.get("tag_path") == "retrieved_text")
    assert rt["value"] == "line3\nline2\nline1"
    # Other tag preserved.
    other = next(t for t in out.tags if t.get("tag_path") == "other")
    assert other["value"] == "v"


@pytest.mark.asyncio
async def test_aggregate_skips_errored_examples() -> None:
    """_aggregate skips EvalExampleResult entries with non-None error."""
    from audio_graphy.eval.types import EvalExampleResult, MetricResult

    per_example = (
        EvalExampleResult(
            example_id="ok",
            metrics=(MetricResult(name="x", value=0.5, denominator=1, details={}),),
            error=None,
        ),
        EvalExampleResult(example_id="bad", metrics=(), error="boom"),
    )
    out = EvalRunner._aggregate(per_example)
    assert "x" in out
    assert out["x"] == 0.5


# ============================================================
# RAGPipeline — branch coverage
# ============================================================


@pytest.mark.asyncio
async def test_rag_predict_empty_answer_yields_empty_entities(tmp_path: Any) -> None:
    """Empty answer text → no entity extraction call, empty entities returned."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from audio_graphy.config import get_settings

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    class _EmptySvc:
        async def search(self, **kwargs: Any) -> dict[str, Any]:
            return {"answer": "", "citations": [], "retrieval_stats": {}}

    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t",
        user_id=1,
        bundle=MagicMock(),
        session_factory=sf,
        query_service=_EmptySvc(),
    )
    pred = await pipeline.predict(_make_gold())
    assert pred.entities == ()
    await engine.dispose()


@pytest.mark.asyncio
async def test_rag_predict_build_query_service_when_missing(tmp_path: Any) -> None:
    """When query_service is None, RAGPipeline builds one. Without vector_store → RuntimeError."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from audio_graphy.config import get_settings

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t",
        user_id=1,
        bundle=MagicMock(),
        session_factory=sf,
        query_service=None,
        graph_store=None,
        vector_store=None,
    )
    # No query_service + no vector_store → RuntimeError.
    with pytest.raises(RuntimeError, match="requires either"):
        await pipeline.predict(_make_gold())
    await engine.dispose()


@pytest.mark.asyncio
async def test_rag_predict_extract_failure_returns_empty_entities(tmp_path: Any) -> None:
    """If entity extraction raises, RAGPipeline.predict still returns a result."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from audio_graphy.config import get_settings

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    class _StubSvc:
        async def search(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "answer": "Some answer text that is non-empty.",
                "citations": [],
                "retrieval_stats": {},
            }

    # Build a bundle whose strong_llm.complete raises.
    bundle = MagicMock()
    bundle.strong_llm.model = "stub"

    async def _raise_complete(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("LLM extraction failure")

    bundle.strong_llm.complete = _raise_complete

    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t",
        user_id=1,
        bundle=bundle,
        session_factory=sf,
        query_service=_StubSvc(),
    )
    pred = await pipeline.predict(_make_gold())
    # Extraction failure → empty entities, but answer still present.
    assert pred.entities == ()
    assert "answer text" in pred.answer
    await engine.dispose()


def _make_gold() -> GoldExample:
    return GoldExample(
        query="Q1",
        gold_answer="A1",
        gold_context_ids=("c1",),
        gold_entities=(("CS75 Plus", "车型"),),
        gold_edges=(),
        gold_tags=(),
    )


@pytest.mark.asyncio
async def test_rag_pipeline_repr(tmp_path: Any) -> None:
    """RAGPipeline __repr__ surfaces tenant + user."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from audio_graphy.config import get_settings

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="tenant_x",
        user_id=42,
        bundle=MagicMock(),
        session_factory=sf,
    )
    s = repr(pipeline)
    assert "tenant_x" in s
    assert "42" in s
    await engine.dispose()


@pytest.mark.asyncio
async def test_mock_pipeline_repr_in_config(tmp_path: Path) -> None:
    """MockPipeline's __repr__ is captured in EvalRunner config."""
    gold_path = _write_gold(tmp_path / "gold.yaml", [_gold_item()])
    runner = EvalRunner(gold_set_path=gold_path, pipeline=MockPipeline(precision=0.5))
    # Pipeline repr in config.
    assert "MockPipeline" in runner._config_snapshot["pipeline"]
    assert "0.5" in runner._config_snapshot["pipeline"]
