"""Contract tests for production query-path LLM gateway call sites."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core import rerank as rerank_module
from audio_graphy.core import retrieval as retrieval_module
from audio_graphy.core import streaming_retrieval as streaming_module
from audio_graphy.core.language_detection import detect_semantic_language
from audio_graphy.core.rerank import Reranker
from audio_graphy.core.retrieval import CandidateSegment, DualChannelRetriever
from audio_graphy.core.streaming_retrieval import StreamingRetriever
from audio_graphy.services.llm_gateway import CachePolicy, LLMRequest


class _Adapter:
    model = "test-model"


class _ForbiddenFileIndex:
    async def get_llm_cache(self, _key: str) -> str | None:
        raise AssertionError("query-path LLM calls must not read FileIndex cache")

    async def set_llm_cache(self, _key: str, _text: str) -> None:
        raise AssertionError("query-path LLM calls must not write FileIndex cache")


def _response(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="test-model", prompt_hash="provider")


def _assert_complete_recipe(request: LLMRequest) -> None:
    assert request.messages
    assert request.prompt_version
    assert request.schema_version
    assert request.parser_version
    assert request.postprocessor_version
    assert request.business_snapshot
    assert request.permission_scope
    assert request.provenance


def _assert_keyword_validator(request: LLMRequest) -> None:
    validator = request.response_validator
    assert validator is not None
    assert validator(_response("CS75,优惠"))
    assert not validator(_response(""))
    assert not validator(_response(" \n\t"))
    assert not validator(_response("关键词："))
    assert not validator(_response("车"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CS75 有什么优惠？", "zh-CN"),
        ("CS75 discount available?", "en"),
        ("café price 2026", "en"),
        ("مرحبا", "und"),
        ("こんにちは", "und"),
        ("12345?!", "und"),
        ("", "und"),
    ],
)
def test_detect_semantic_language_is_deterministic(text: str, expected: str) -> None:
    assert detect_semantic_language(text) == expected


@pytest.mark.unit
class TestRetrievalKeywordGateway:
    async def test_uses_semantic_policy_and_tenant_scoped_sha256_recipe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            return _response("CS75,优惠")

        monkeypatch.setattr(retrieval_module, "execute_llm", capture)
        bundle = SimpleNamespace(weak_llm=_Adapter())
        retriever = DualChannelRetriever(
            bundle,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            file_index=_ForbiddenFileIndex(),  # type: ignore[arg-type]
        )
        permission_scope = {"role": "manager", "store_ids": ["north"]}

        first = await retriever._extract_keywords(
            "CS75 有什么优惠？",
            tenant_id="tenant-a",
            permission_scope=permission_scope,
        )
        await retriever._extract_keywords(
            "CS75 discount available?",
            tenant_id="tenant-b",
            permission_scope=permission_scope,
        )

        assert first == ["CS75", "优惠"]
        assert [request.tenant_id for request in captured] == ["tenant-a", "tenant-b"]
        request = captured[0]
        assert request.purpose == "keyword_extract"
        assert request.model_tier == "weak"
        assert request.cache_policy is CachePolicy.QUERY_SEMANTIC
        assert request.ttl_seconds == 7 * 24 * 60 * 60
        assert request.permission_scope == permission_scope
        assert request.semantic_language == "zh-CN"
        assert captured[1].semantic_language == "en"
        _assert_keyword_validator(request)
        _assert_complete_recipe(request)
        assert request.recipe_sha256(model="test-model") != captured[1].recipe_sha256(
            model="test-model"
        )


@pytest.mark.unit
class TestStreamingKeywordGateway:
    async def test_carries_tenant_session_permission_and_query_provenance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            return _response("客户A,价格")

        monkeypatch.setattr(streaming_module, "execute_llm", capture)
        bundle = SimpleNamespace(weak_llm=_Adapter())
        retriever = StreamingRetriever(
            lambda _tenant: object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            bundle,  # type: ignore[arg-type]
        )

        keywords = await retriever._extract_keywords(
            "客户A 问了什么？",
            tenant_id="tenant-live",
            session_id="session-42",
            permission_scope={"role": "agent", "store_ids": ["s-1"]},
        )
        await retriever._extract_keywords(
            "customer price question?",
            tenant_id="tenant-live",
        )

        assert keywords == ["客户A", "价格"]
        request = captured[0]
        assert request.tenant_id == "tenant-live"
        assert request.purpose == "keyword_extract"
        assert request.model_tier == "weak"
        assert request.cache_policy is CachePolicy.QUERY_SEMANTIC
        assert request.semantic_language == "zh-CN"
        assert captured[1].semantic_language == "en"
        _assert_keyword_validator(request)
        assert any(
            getattr(item, "source_type", None) == "streaming_session"
            and getattr(item, "source_id", None) == "session-42"
            for item in request.provenance
        )
        _assert_complete_recipe(request)


@pytest.mark.unit
class TestRerankGateway:
    async def test_relevance_and_final_answer_have_distinct_exact_ttls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            if request.purpose == "relevance_judge":
                return _response("yes")
            if request.purpose == "final_answer":
                return _response("最终回答 [1]")
            raise AssertionError(f"unexpected purpose: {request.purpose}")

        monkeypatch.setattr(rerank_module, "execute_llm", capture)
        bundle = SimpleNamespace(strong_llm=_Adapter(), weak_llm=_Adapter())
        reranker = Reranker(
            bundle,  # type: ignore[arg-type]
            file_index=_ForbiddenFileIndex(),  # type: ignore[arg-type]
        )
        candidate = CandidateSegment(
            chunk_id=9,
            recording_id=3,
            segment_ids=[7, 8],
            text="客户询问 CS75 的优惠，坐席回答有两万元补贴。",
            recorded_at=datetime(2026, 7, 20, tzinfo=UTC),
            score=0.9,
            source_channel="naive",
        )
        permission_scope = {"role": "manager", "store_ids": ["north"]}

        result = await reranker.rerank_and_answer(
            "CS75 有什么优惠？",
            [candidate],
            tenant_id="tenant-a",
            permission_scope=permission_scope,
            keywords=("CS75", "优惠"),
        )

        assert result.answer == "最终回答 [1]"
        assert [request.purpose for request in captured] == [
            "relevance_judge",
            "final_answer",
        ]
        relevance, final = captured
        assert relevance.ttl_seconds == 7 * 24 * 60 * 60
        assert final.ttl_seconds == 5 * 60
        assert relevance.response_validator is not None
        assert relevance.response_validator(_response("yes"))
        assert not relevance.response_validator(_response("maybe"))
        assert not relevance.response_validator(_response("not yes"))
        assert final.response_validator is not None
        assert final.response_validator(_response("最终回答 [1]"))
        assert not final.response_validator(_response(""))
        for request in captured:
            assert request.tenant_id == "tenant-a"
            assert request.model_tier == "strong"
            assert request.cache_policy is CachePolicy.EXACT
            assert request.permission_scope == permission_scope
            _assert_complete_recipe(request)
        assert any(
            getattr(item, "source_type", None) == "recording"
            and getattr(item, "source_id", None) == "3"
            for item in final.provenance
        )

    async def test_keyword_fallback_uses_weak_semantic_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            return _response("CS75,优惠")

        monkeypatch.setattr(rerank_module, "execute_llm", capture)
        bundle = SimpleNamespace(strong_llm=_Adapter(), weak_llm=_Adapter())
        reranker = Reranker(bundle)  # type: ignore[arg-type]

        assert await reranker._extract_keywords(
            "CS75 有什么优惠？",
            tenant_id="tenant-a",
            permission_scope={"role": "manager"},
        ) == ["CS75", "优惠"]
        await reranker._extract_keywords(
            "CS75 discount available?",
            tenant_id="tenant-a",
            permission_scope={"role": "manager"},
        )

        request = captured[0]
        assert request.model_tier == "weak"
        assert request.purpose == "keyword_extract"
        assert request.cache_policy is CachePolicy.QUERY_SEMANTIC
        assert request.semantic_text == "CS75 有什么优惠？"
        assert request.semantic_language == "zh-CN"
        assert captured[1].semantic_language == "en"
        _assert_keyword_validator(request)
        _assert_complete_recipe(request)
