"""Dataclasses for the evaluation subsystem — frozen + slots everywhere.

评估子系统数据模型 —— 全部 frozen + slots；与 PRD §5.1 严格对齐。

These types are intentionally decoupled from `audio_graphy.adapters.protocols`:
- EdgeConfidence is re-declared as a Literal alias so `eval/` can be type-checked
  and consumed without importing the adapters package.
- Tuples (not lists) are used throughout to preserve immutability and allow
  hash-based caching / set operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Confidence tag attached to every edge in the knowledge graph.
# Mirrors `audio_graphy.adapters.protocols.EdgeConfidence` — kept independent
# so eval/ does not depend on adapters/.
EdgeConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]


@dataclass(frozen=True, slots=True)
class GoldExample:
    """One QA-style eval example / 一条评估样本（黄金集）.

    Attributes:
        query: User question / 用户提问.
        gold_answer: Reference answer / 标准答案.
        gold_context_ids: Ground-truth chunk IDs for retrieval metrics.
        gold_entities: ``(entity_text, entity_type)`` tuples.
        gold_edges: ``(src, rel, dst, confidence)`` 4-tuples.
        gold_tags: ``{"tag_path": ..., "value": ...}`` dicts.
        recording_id: Optional audio recording ID for end-to-end runs.
        metadata: Free-form scenario metadata (tenant / scenario / etc.).
    """

    query: str
    gold_answer: str
    gold_context_ids: tuple[str, ...]
    gold_entities: tuple[tuple[str, str], ...]
    gold_edges: tuple[tuple[str, str, str, EdgeConfidence], ...]
    gold_tags: tuple[dict[str, str], ...]
    recording_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PredictedResult:
    """Pipeline output for a single GoldExample / 单条预测结果."""

    query: str
    answer: str
    retrieved_context_ids: tuple[str, ...]
    entities: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str, str, EdgeConfidence], ...]
    tags: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class MetricResult:
    """Result of one metric for one example / 单项指标结果.

    Attributes:
        name: Metric identifier (e.g. ``"context_precision_at_5"``).
        value: Score in ``[0.0, 1.0]`` unless otherwise noted.
        denominator: Weight used when aggregating (typically 1 per example).
        details: Extra bookkeeping (``denominator_zero``, ``reason``, etc.).
    """

    name: str
    value: float
    denominator: int
    details: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalExampleResult:
    """All metrics for one (GoldExample, PredictedResult) pair / 单条样本评估结果."""

    example_id: str
    metrics: tuple[MetricResult, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One full evaluation run / 一次完整评估.

    Attributes:
        run_id: Short identifier (UUID4 hex[:12]).
        gold_set_path: Path to the YAML gold set.
        started_at: ISO 8601 timestamp.
        finished_at: ISO 8601 timestamp.
        config: Snapshot of relevant Settings (model names, k, etc.).
        aggregate_metrics: Arithmetic mean of each metric across examples.
        per_example: All per-example results, in gold-set order.
    """

    run_id: str
    gold_set_path: str
    started_at: str
    finished_at: str
    config: dict[str, str]
    aggregate_metrics: dict[str, float]
    per_example: tuple[EvalExampleResult, ...]


__all__ = [
    "EdgeConfidence",
    "EvalExampleResult",
    "EvalRun",
    "GoldExample",
    "MetricResult",
    "PredictedResult",
]
