"""Shared fixtures + stub LLMJudge for eval metric tests.

Fixtures (per arch doc §7.4):
- gold_smoke: typical CS75 Plus scenario.
- pred_perfect: identical to gold (all metrics → 1.0).
- pred_empty: empty answer / retrieval (worst case).
- StubJudge: no-network LLMJudge stub with configurable return values.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from audio_graphy.eval.types import GoldExample, PredictedResult


class StubJudge(Protocol):
    """Structural type matching LLMJudge methods used by generation metrics."""

    async def extract_facts(self, text: str) -> list[str]: ...
    async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]: ...
    async def judge_relevance(self, query: str, answer: str) -> float: ...


class _StubJudgeImpl:
    """No-network stub implementing the LLMJudge protocol.

    All methods are async (match the real LLMJudge signature).
    """

    def __init__(
        self,
        *,
        facts: list[str] | None = None,
        flags: list[bool] | None = None,
        score: float = 1.0,
    ) -> None:
        self._facts = list(facts) if facts is not None else ["fact-A", "fact-B"]
        self._flags = list(flags) if flags is not None else [True, True]
        self._score = score

    async def extract_facts(self, text: str) -> list[str]:
        del text
        return list(self._facts)

    async def judge_faithfulness(self, context: str, facts: list[str]) -> list[bool]:
        del context
        n = len(facts)
        if not self._flags:
            return [False] * n
        padded = list(self._flags) + [False] * max(0, n - len(self._flags))
        return padded[:n]

    async def judge_relevance(self, query: str, answer: str) -> float:
        del query, answer
        return float(self._score)


@pytest.fixture
def gold_smoke() -> GoldExample:
    """Typical CS75 Plus gold example."""
    return GoldExample(
        query="CS75 Plus 七月优惠多少？",
        gold_answer="5 万元现金优惠 + 2 年免息分期。",
        gold_context_ids=("chunk-001", "chunk-004"),
        gold_entities=(
            ("CS75 Plus", "车型"),
            ("5万", "价格方案"),
        ),
        gold_edges=(
            ("坐席", "推荐", "CS75 Plus", "EXTRACTED"),
            ("CS75 Plus", "搭配", "2年免息", "INFERRED"),
        ),
        gold_tags=(
            {"tag_path": "接待.价格.优惠", "value": "5万"},
            {"tag_path": "接待.金融.免息", "value": "2年"},
        ),
    )


@pytest.fixture
def pred_perfect(gold_smoke: GoldExample) -> PredictedResult:
    """Prediction identical to gold → all metrics should be 1.0."""
    return PredictedResult(
        query=gold_smoke.query,
        answer=gold_smoke.gold_answer,
        retrieved_context_ids=gold_smoke.gold_context_ids,
        entities=gold_smoke.gold_entities,
        edges=gold_smoke.gold_edges,
        tags=gold_smoke.gold_tags,
    )


@pytest.fixture
def pred_empty(gold_smoke: GoldExample) -> PredictedResult:
    """Empty prediction → worst case (all non-LLM metrics 0.0 except entity_f1)."""
    return PredictedResult(
        query=gold_smoke.query,
        answer="",
        retrieved_context_ids=(),
        entities=(),
        edges=(),
        tags=(),
    )


@pytest.fixture
def stub_judge() -> _StubJudgeImpl:
    """Default stub: 2 facts, both supported, relevance 1.0."""
    return _StubJudgeImpl(
        facts=["fact-A", "fact-B"],
        flags=[True, True],
        score=1.0,
    )


def make_stub(
    *,
    facts: list[str] | None = None,
    flags: list[bool] | None = None,
    score: float = 1.0,
) -> _StubJudgeImpl:
    """Factory for inline stub creation in test bodies."""
    return _StubJudgeImpl(facts=facts, flags=flags, score=score)
