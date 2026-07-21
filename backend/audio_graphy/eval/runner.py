"""EvalRunner — runs metrics over a gold set against an EvalPipeline.

评估运行器：加载 gold set YAML → 并发跑每个 example 的 8 项指标 → 聚合。

Pipeline protocol (PRD §5.2):
    async def predict(gold: GoldExample) -> PredictedResult

Built-in pipelines:
- ``MockPipeline(precision=1.0)`` — echoes gold (M5 default; for smoke testing).
- ``RAGPipeline`` — M6 stub, raises NotImplementedError.

Concurrency: asyncio.Semaphore bound (default 4 from settings.eval_concurrency).
Error tolerance: per-example exceptions are captured into EvalExampleResult.error
  and the example is excluded from the aggregate mean.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

from audio_graphy.eval.metrics.audio_graphy import (
    edge_precision_by_confidence,
    entity_f1,
    tag_accuracy,
)
from audio_graphy.eval.metrics.retrieval import context_precision_at_k, context_recall
from audio_graphy.eval.types import (
    EvalExampleResult,
    EvalRun,
    GoldExample,
    MetricResult,
    PredictedResult,
)

if TYPE_CHECKING:
    from audio_graphy.config import Settings
    from audio_graphy.eval.judge import LLMJudge

logger = logging.getLogger(__name__)

_DEFAULT_K = 5


# ============================================================
# Pipeline protocol + built-ins
# ============================================================


class EvalPipeline(Protocol):
    """Abstract pipeline: produces predictions for a gold example."""

    async def predict(self, gold: GoldExample) -> PredictedResult: ...


class MockPipeline:
    """Echoes gold back as the prediction — for testing metrics/reporter.

    Args:
        precision: 1.0 → echo gold (perfect score); 0.0 → empty prediction.
    """

    def __init__(self, precision: float = 1.0) -> None:
        self.precision = precision

    async def predict(self, gold: GoldExample) -> PredictedResult:
        if self.precision >= 1.0:
            return PredictedResult(
                query=gold.query,
                answer=gold.gold_answer,
                retrieved_context_ids=gold.gold_context_ids,
                entities=gold.gold_entities,
                edges=gold.gold_edges,
                tags=gold.gold_tags,
            )
        return PredictedResult(
            query=gold.query,
            answer="",
            retrieved_context_ids=(),
            entities=(),
            edges=(),
            tags=(),
        )

    def __repr__(self) -> str:
        return f"MockPipeline(precision={self.precision})"


# ============================================================
# Runner
# ============================================================


class EvalRunner:
    """Runs metrics over a gold set against a pipeline.

    Args:
        gold_set_path: Path to a YAML file containing a list of gold examples.
        pipeline: Any object implementing ``EvalPipeline``.
        judge: Optional LLMJudge; when ``None``, faithfulness / answer_relevance
            / factual_correctness are skipped (recorded as 0.0 with
            ``details.skipped=True``).
        settings: Used to read ``eval_concurrency``. If ``None``, uses 4.
        k: Cutoff for ``context_precision_at_k`` (default 5).
        config_snapshot: Optional dict merged into ``EvalRun.config``.
    """

    def __init__(
        self,
        *,
        gold_set_path: Path,
        pipeline: EvalPipeline,
        judge: LLMJudge | None = None,
        settings: Settings | None = None,
        k: int = _DEFAULT_K,
        config_snapshot: dict[str, str] | None = None,
    ) -> None:
        self._gold_set_path = Path(gold_set_path)
        self._pipeline = pipeline
        self._judge = judge
        self._k = k
        concurrency = settings.eval_concurrency if settings is not None else 4
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._config_snapshot = dict(config_snapshot or {})
        self._config_snapshot.setdefault(
            "pipeline", type(pipeline).__name__ + f"(precision={getattr(pipeline, 'precision', 'n/a')})"
        )
        self._config_snapshot.setdefault("k", str(k))
        self._config_snapshot.setdefault("judge", "enabled" if judge is not None else "disabled")

    async def run(self) -> EvalRun:
        started_at = datetime.now(UTC).isoformat()
        run_id = uuid.uuid4().hex[:12]
        examples = self._load_gold_set()
        tasks = [self._eval_one(ex, idx) for idx, ex in enumerate(examples)]
        per_example = tuple(await asyncio.gather(*tasks))
        aggregate = self._aggregate(per_example)
        finished_at = datetime.now(UTC).isoformat()
        return EvalRun(
            run_id=run_id,
            gold_set_path=str(self._gold_set_path),
            started_at=started_at,
            finished_at=finished_at,
            config=self._config_snapshot,
            aggregate_metrics=aggregate,
            per_example=per_example,
        )

    # ----------------------------------------------------------
    # Per-example evaluation
    # ----------------------------------------------------------
    async def _eval_one(self, gold: GoldExample, idx: int) -> EvalExampleResult:
        example_id = f"ex-{idx + 1:03d}"
        try:
            async with self._semaphore:
                pred = await self._pipeline.predict(gold)
        except Exception as exc:
            logger.error("Pipeline crashed on %s: %s", example_id, exc)
            return EvalExampleResult(example_id=example_id, metrics=(), error=repr(exc))

        try:
            metrics = await self._compute_metrics(gold, pred)
        except Exception as exc:
            logger.error("Metric failed on %s: %s", example_id, exc)
            return EvalExampleResult(example_id=example_id, metrics=(), error=repr(exc))

        return EvalExampleResult(example_id=example_id, metrics=tuple(metrics), error=None)

    async def _compute_metrics(
        self, gold: GoldExample, pred: PredictedResult
    ) -> list[MetricResult]:
        # Retrieval metrics (no LLM).
        results: list[MetricResult] = [
            context_precision_at_k(gold, pred, k=self._k),
            context_recall(gold, pred),
        ]

        # AudioGraphy-specific metrics (no LLM).
        results.extend([
            entity_f1(gold, pred),
            edge_precision_by_confidence(gold, pred),
            tag_accuracy(gold, pred),
        ])

        # LLM-backed metrics — skipped when judge is None.
        if self._judge is None:
            for name in ("faithfulness", "answer_relevance", "factual_correctness"):
                results.append(MetricResult(
                    name=name, value=0.0, denominator=0, details={"skipped": True},
                ))
        else:
            from audio_graphy.eval.metrics.generation import (
                answer_relevance,
                factual_correctness,
                faithfulness,
            )
            results.append(await faithfulness(gold, pred, self._judge))
            results.append(await answer_relevance(gold, pred, self._judge))
            results.append(await factual_correctness(gold, pred, self._judge))

        return results

    # ----------------------------------------------------------
    # Aggregation
    # ----------------------------------------------------------
    @staticmethod
    def _aggregate(per_example: tuple[EvalExampleResult, ...]) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for ex in per_example:
            if ex.error is not None:
                continue
            for m in ex.metrics:
                buckets.setdefault(m.name, []).append(m.value)
        return {name: sum(vals) / len(vals) for name, vals in buckets.items() if vals}

    # ----------------------------------------------------------
    # Gold set loading
    # ----------------------------------------------------------
    def _load_gold_set(self) -> list[GoldExample]:
        if not self._gold_set_path.is_file():
            raise FileNotFoundError(f"gold set not found: {self._gold_set_path}")
        raw = yaml.safe_load(self._gold_set_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(
                f"gold set must be a YAML list, got {type(raw).__name__}"
            )
        return [self._gold_from_dict(item, i) for i, item in enumerate(raw)]

    @staticmethod
    def _gold_from_dict(item: object, idx: int) -> GoldExample:
        if not isinstance(item, dict):
            raise ValueError(f"gold[{idx}] is not a mapping: {item!r}")
        try:
            return GoldExample(
                query=str(item["query"]),
                gold_answer=str(item["gold_answer"]),
                gold_context_ids=tuple(str(x) for x in item.get("gold_context_ids", [])),
                gold_entities=tuple(
                    (str(t), str(y)) for t, y in item.get("gold_entities", [])
                ),
                gold_edges=tuple(
                    (str(s), str(r), str(d), str(c))  # type: ignore[misc]
                    for s, r, d, c in item.get("gold_edges", [])
                ),
                gold_tags=tuple(
                    {str(k): str(v) for k, v in dict(t).items()}
                    for t in item.get("gold_tags", [])
                ),
                recording_id=(
                    str(item["recording_id"]) if item.get("recording_id") else None
                ),
                metadata={
                    str(k): str(v) for k, v in dict(item.get("metadata", {})).items()
                },
            )
        except KeyError as exc:
            raise ValueError(f"gold[{idx}] missing required key: {exc}") from exc


__all__ = ["EvalPipeline", "EvalRunner", "MockPipeline"]
