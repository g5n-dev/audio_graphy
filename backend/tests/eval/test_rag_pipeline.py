"""Tests for ``RAGPipeline`` — the M6 real-pipeline implementation.

Mocks ``QueryService`` and ``AdapterBundle`` so no network is required.

Cases:
    1. ``predict()`` returns a full ``PredictedResult`` with extracted
       entities when the LLM emits a GraphRAG-style entity record.
    2. Empty retrieval (no citations) → empty ``retrieved_context_ids``.
    3. LLM/extractor failure → caught and returned as empty entity list
       (does not raise — pipeline degrades gracefully).
    4. Entity extraction is invoked on the answer text.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.eval.runner import RAGPipeline
from audio_graphy.eval.types import GoldExample, PredictedResult


class _MockQueryService:
    """Mock QueryService that returns a fixed ``search`` result."""

    def __init__(self, answer: str, citations: list[dict[str, Any]]) -> None:
        self._answer = answer
        self._citations = citations
        self.captured_queries: list[str] = []

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int = 5,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        self.captured_queries.append(query)
        return {
            "query": query,
            "answer": self._answer,
            "citations": self._citations,
            "retrieval_stats": {
                "naive_hits": len(self._citations),
                "graph_hits": 0,
            },
        }


def _stub_bundle(extract_text: str | None = "test answer") -> Any:
    """Build an AdapterBundle with a stub strong_llm.

    The stub's ``complete`` returns an LLMResponse whose ``.text`` is the
    raw GraphRAG-formatted extraction record. If ``extract_text`` is
    ``None`` the stub raises to simulate LLM failure.
    """
    bundle = MagicMock()
    bundle.strong_llm.model = "stub-model"

    async def _complete(messages, *, temperature=0.0, max_tokens=None, cache_key=None):
        if extract_text is None:
            raise RuntimeError("LLM exploded")
        return LLMResponse(
            text=extract_text,
            model="stub-model",
            prompt_hash="stub",
            cached=False,
            usage={},
        )

    bundle.strong_llm.complete = _complete
    return bundle


def _make_gold() -> GoldExample:
    return GoldExample(
        query="Q1",
        gold_answer="A1",
        gold_context_ids=("c1",),
        gold_entities=(("CS75 Plus", "车型"),),
        gold_edges=(),
        gold_tags=(),
    )


# GraphRAG-format extraction record that ``EntityExtractor`` parses.
# Records are separated by RECORD_DELIMITER ("##") and the whole output
# ends with COMPLETION_DELIMITER ("<|COMPLETE|>"). Fields inside a record
# are split by TUPLE_DELIMITER ("<|>").
_EXTRACT_RESPONSE = (
    '("实体"<|>CS75 Plus<|>车型<|>紧凑型 SUV)'
    "##"
    '("实体"<|>客户A<|>客户<|>购车意向)'
    "##"
    "<|COMPLETE|>"
)


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_predict_returns_full_predicted_result(tmp_path: Any) -> None:
    """predict() builds a PredictedResult with entities extracted from answer."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.ext.asyncio import AsyncSession

    from audio_graphy.config import get_settings

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    svc = _MockQueryService(
        answer="我们推荐 CS75 Plus，适合家庭使用。",
        citations=[
            {"chunk_id": 1, "transcript_snippet": "客户询问 CS75 Plus 价格"},
            {"chunk_id": 2, "transcript_snippet": "销售推荐家庭用车"},
        ],
    )
    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t1",
        user_id=1,
        bundle=_stub_bundle(_EXTRACT_RESPONSE),
        session_factory=sf,
        query_service=svc,
    )
    pred = await pipeline.predict(_make_gold())
    assert isinstance(pred, PredictedResult)
    assert pred.query == "Q1"
    assert "CS75 Plus" in pred.answer
    assert tuple(pred.retrieved_context_ids) == ("1", "2")
    # At least one entity was extracted.
    assert any(name == "CS75 Plus" for name, _ in pred.entities)
    # retrieved_text tag is populated for the faithfulness metric.
    retrieved = [t for t in pred.tags if t.get("tag_path") == "retrieved_text"]
    assert retrieved, "retrieved_text tag should be present"
    await engine.dispose()


@pytest.mark.asyncio
async def test_predict_empty_retrieval_yields_empty_ids(tmp_path: Any) -> None:
    """No citations → empty retrieved_context_ids."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.ext.asyncio import AsyncSession

    from audio_graphy.config import get_settings

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    svc = _MockQueryService(answer="No relevant info found.", citations=[])
    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t1",
        user_id=1,
        bundle=_stub_bundle(_EXTRACT_RESPONSE),
        session_factory=sf,
        query_service=svc,
    )
    pred = await pipeline.predict(_make_gold())
    assert pred.retrieved_context_ids == ()
    await engine.dispose()


@pytest.mark.asyncio
async def test_predict_llm_failure_returns_empty_entities(tmp_path: Any) -> None:
    """LLM failure during extraction → caught, entities=[] but pipeline runs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.ext.asyncio import AsyncSession

    from audio_graphy.config import get_settings

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    svc = _MockQueryService(answer="Some text", citations=[])
    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t1",
        user_id=1,
        # extract_text=None forces the stub to raise.
        bundle=_stub_bundle(None),
        session_factory=sf,
        query_service=svc,
    )
    # Should NOT raise — pipeline degrades to empty entities.
    pred = await pipeline.predict(_make_gold())
    assert pred.entities == ()
    await engine.dispose()


@pytest.mark.asyncio
async def test_predict_invokes_entity_extraction(tmp_path: Any) -> None:
    """The EntityExtractor is invoked on the answer text."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.ext.asyncio import AsyncSession

    from audio_graphy.config import get_settings

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    settings = get_settings()

    svc = _MockQueryService(
        answer="今天聊到 CS75 Plus 这款车。",
        citations=[],
    )
    pipeline = RAGPipeline(
        settings=settings,
        tenant_id="t1",
        user_id=1,
        bundle=_stub_bundle(_EXTRACT_RESPONSE),
        session_factory=sf,
        query_service=svc,
    )
    pred = await pipeline.predict(_make_gold())
    # Entity extraction was called: svc.search got the query, AND the
    # answer text was processed (entities extracted even with no citations).
    assert svc.captured_queries == ["Q1"]
    assert len(pred.entities) > 0
    await engine.dispose()
