"""Retrieval metrics — Context Precision@k and Context Recall.

Both are pure functions over ``gold_context_ids`` vs ``retrieved_context_ids``.
No LLM calls; order-insensitive via set intersection.

Formulas (PRD §5.3.1):
- Context Precision@k = |gold ∩ retrieved[:k]| / min(k, len(gold))
- Context Recall      = |gold ∩ retrieved_all| / len(gold)

Edge cases (PRD §5.3.3):
- gold empty OR k<=0  → value=0.0, details["denominator_zero"]=True
- retrieved empty     → naturally 0/positive = 0.0 (no special flag)
"""

from __future__ import annotations

from audio_graphy.eval.types import GoldExample, MetricResult, PredictedResult


def context_precision_at_k(gold: GoldExample, pred: PredictedResult, *, k: int = 5) -> MetricResult:
    """Top-k precision of retrieved chunks against gold chunks.

    Args:
        gold: Gold example containing ``gold_context_ids``.
        pred: Pipeline prediction containing ``retrieved_context_ids`` (ranked).
        k: Cutoff rank; only the first ``k`` retrieved IDs count.

    Returns:
        MetricResult with ``name=f"context_precision_at_{k}"``.
        ``details`` carries ``k / gold_count / retrieved_count / hits`` for
        transparent reporting. ``denominator_zero=True`` when gold empty or k<=0.
    """
    gold_set = set(gold.gold_context_ids)
    retrieved_top_k = list(pred.retrieved_context_ids[:k])

    if k <= 0 or not gold_set:
        return MetricResult(
            name=f"context_precision_at_{k}",
            value=0.0,
            denominator=0,
            details={
                "k": k,
                "gold_count": len(gold_set),
                "retrieved_count": len(retrieved_top_k),
                "hits": 0,
                "denominator_zero": True,
            },
        )

    retrieved_set_top_k = set(retrieved_top_k)
    hits = len(gold_set & retrieved_set_top_k)
    denom = min(k, len(gold_set))
    value = hits / denom if denom > 0 else 0.0

    return MetricResult(
        name=f"context_precision_at_{k}",
        value=value,
        denominator=denom,
        details={
            "k": k,
            "gold_count": len(gold_set),
            "retrieved_count": len(retrieved_top_k),
            "hits": hits,
        },
    )


def context_recall(gold: GoldExample, pred: PredictedResult) -> MetricResult:
    """Recall = (# gold chunks found anywhere in retrieved) / len(gold).

    Args:
        gold: Gold example containing ``gold_context_ids``.
        pred: Pipeline prediction containing ``retrieved_context_ids`` (full list).

    Returns:
        MetricResult named ``"context_recall"``.
        ``denominator_zero=True`` when gold is empty.
    """
    gold_set = set(gold.gold_context_ids)
    retrieved_set = set(pred.retrieved_context_ids)

    if not gold_set:
        return MetricResult(
            name="context_recall",
            value=0.0,
            denominator=0,
            details={
                "gold_count": 0,
                "retrieved_count": len(retrieved_set),
                "hits": 0,
                "denominator_zero": True,
            },
        )

    hits = len(gold_set & retrieved_set)
    denom = len(gold_set)
    value = hits / denom

    return MetricResult(
        name="context_recall",
        value=value,
        denominator=denom,
        details={
            "gold_count": denom,
            "retrieved_count": len(retrieved_set),
            "hits": hits,
        },
    )


__all__ = ["context_precision_at_k", "context_recall"]
