"""Generation metrics — Faithfulness, Answer Relevance, Factual Correctness.

All three are pure functions that take a ``judge`` argument (LLM-backed). The
judge is injected so metric unit tests can use a no-network stub.

LLMJudge Protocol (duck-typed):
    class Judge(Protocol):
        async def extract_facts(self, text: str) -> list[str]: ...
        async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]: ...
        async def judge_relevance(self, query: str, answer: str) -> float: ...

Formulas (PRD §5.3.1):
- Faithfulness        = (supported facts) / (total facts in answer)
- Answer Relevance    = judge.judge_relevance(query, answer) ∈ {0, 0.5, 1}
- Factual Correctness = F1(precision, recall) over fact sets

Edge cases (PRD §5.3.3):
- pred.answer empty       → faithfulness/relevance = 0.0, reason="empty_answer"
- retrieved_context empty → faithfulness = 0.0, reason="empty_context"
- facts == []             → faithfulness = 0.0, reason="no_facts_extracted"
- facts both empty (factual_correctness) → 1.0, reason="both_empty"
"""

from __future__ import annotations

from typing import Protocol

from audio_graphy.eval.types import GoldExample, MetricResult, PredictedResult


class LLMJudge(Protocol):
    """Subset of the LLMJudge interface required by generation metrics."""

    async def extract_facts(self, text: str) -> list[str]: ...
    async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]: ...
    async def judge_relevance(self, query: str, answer: str) -> float: ...


def _lookup_tag_value(tags: tuple[dict[str, str], ...], key: str) -> str:
    """Return the value of the first tag whose ``tag_path == key``, else ""."""
    for tag in tags:
        if tag.get("tag_path") == key:
            return str(tag.get("value", ""))
    return ""


async def faithfulness(gold: GoldExample, pred: PredictedResult, judge: LLMJudge) -> MetricResult:
    """Faithfulness = supported_facts / total_facts.

    The retrieved context text is read from ``pred.tags`` (key
    ``"retrieved_text"``); the pipeline is responsible for stamping this tag.

    Args:
        gold: unused except for typing symmetry; kept for API uniformity.
        pred: Pipeline prediction (answer + tags).
        judge: LLMJudge instance for fact extraction + verdict.

    Returns:
        MetricResult named ``"faithfulness"``.
    """
    del gold  # unused — signature symmetry with other metrics

    if not pred.answer.strip():
        return MetricResult(
            name="faithfulness",
            value=0.0,
            denominator=1,
            details={"reason": "empty_answer", "facts_count": 0},
        )

    context_text = _lookup_tag_value(pred.tags, "retrieved_text")
    if not context_text.strip():
        return MetricResult(
            name="faithfulness",
            value=0.0,
            denominator=1,
            details={"reason": "empty_context", "facts_count": 0},
        )

    facts = await judge.extract_facts(pred.answer)
    if not facts:
        return MetricResult(
            name="faithfulness",
            value=0.0,
            denominator=1,
            details={"reason": "no_facts_extracted", "facts_count": 0},
        )

    flags = await judge.judge_faithfulness(context_text, facts)
    # Guard against judge returning a shorter/longer list.
    n = min(len(facts), len(flags))
    supported = sum(1 for f in flags[:n] if f)
    value = supported / len(facts)

    return MetricResult(
        name="faithfulness",
        value=value,
        denominator=len(facts),
        details={
            "facts_count": len(facts),
            "supported_count": supported,
        },
    )


async def answer_relevance(
    gold: GoldExample, pred: PredictedResult, judge: LLMJudge
) -> MetricResult:
    """Answer relevance ∈ {0.0, 0.5, 1.0} as scored by the LLM judge.

    Args:
        gold: Provides the reference ``query``.
        pred: Pipeline prediction (answer).
        judge: LLMJudge instance.

    Returns:
        MetricResult named ``"answer_relevance"``.
    """
    if not pred.answer.strip():
        return MetricResult(
            name="answer_relevance",
            value=0.0,
            denominator=1,
            details={"reason": "empty_answer"},
        )

    # Prefer pred.query (what the pipeline actually answered) when present;
    # fall back to gold.query.
    query = pred.query or gold.query
    value = await judge.judge_relevance(query, pred.answer)

    return MetricResult(
        name="answer_relevance",
        value=float(value),
        denominator=1,
        details={"query": query[:80]},
    )


async def factual_correctness(
    gold: GoldExample, pred: PredictedResult, judge: LLMJudge
) -> MetricResult:
    """Factual correctness = F1 over fact sets extracted from answer vs gold.

    Edge case (PRD §5.3.3): both fact sets empty → 1.0, reason="both_empty".
    """
    facts_pred = await judge.extract_facts(pred.answer)
    facts_gold = await judge.extract_facts(gold.gold_answer)

    set_pred = set(facts_pred)
    set_gold = set(facts_gold)

    if not set_pred and not set_gold:
        return MetricResult(
            name="factual_correctness",
            value=1.0,
            denominator=1,
            details={
                "reason": "both_empty",
                "facts_pred": 0,
                "facts_gold": 0,
                "tp": 0,
            },
        )

    tp = len(set_pred & set_gold)
    precision = tp / len(set_pred) if set_pred else 0.0
    recall = tp / len(set_gold) if set_gold else 0.0
    value = 0.0 if precision + recall <= 0.0 else 2 * precision * recall / (precision + recall)

    return MetricResult(
        name="factual_correctness",
        value=value,
        denominator=1,
        details={
            "facts_pred": len(set_pred),
            "facts_gold": len(set_gold),
            "tp": tp,
            "precision": precision,
            "recall": recall,
        },
    )


__all__ = ["LLMJudge", "answer_relevance", "factual_correctness", "faithfulness"]
