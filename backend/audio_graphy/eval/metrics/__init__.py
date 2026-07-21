"""Metric functions for the evaluation subsystem — all pure functions.

评估指标函数 —— 全部纯函数，无 I/O；LLM 调用通过 ``LLMJudge`` 参数注入。

Public API:
    Retrieval (no LLM):
    - context_precision_at_k(gold, pred, *, k=5) -> MetricResult
    - context_recall(gold, pred) -> MetricResult

    Generation (LLM-backed, judge injected):
    - faithfulness(gold, pred, judge) -> MetricResult
    - answer_relevance(gold, pred, judge) -> MetricResult
    - factual_correctness(gold, pred, judge) -> MetricResult

    AudioGraphy-specific (no LLM):
    - entity_f1(gold, pred) -> MetricResult
    - edge_precision_by_confidence(gold, pred) -> MetricResult
    - tag_accuracy(gold, pred) -> MetricResult
"""

from __future__ import annotations

from audio_graphy.eval.metrics.audio_graphy import (
    edge_precision_by_confidence,
    entity_f1,
    tag_accuracy,
)
from audio_graphy.eval.metrics.generation import (
    answer_relevance,
    factual_correctness,
    faithfulness,
)
from audio_graphy.eval.metrics.retrieval import (
    context_precision_at_k,
    context_recall,
)

__all__ = [
    "answer_relevance",
    "context_precision_at_k",
    "context_recall",
    "edge_precision_by_confidence",
    "entity_f1",
    "factual_correctness",
    "faithfulness",
    "tag_accuracy",
]
