"""Rich LLM request contracts for entity extraction and evaluation judging."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core import extractor as extractor_module
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.types import COMPLETION_DELIMITER, TUPLE_DELIMITER
from audio_graphy.eval import judge as judge_module
from audio_graphy.eval.judge import LLMJudge
from audio_graphy.services.llm_gateway import CachePolicy, LLMRequest


class _Adapter:
    model = "test-model"


class _ForbiddenFileIndex:
    async def get_llm_cache(self, _key: str) -> str | None:
        raise AssertionError("EntityExtractor must not read FileIndex LLM cache")

    async def set_llm_cache(self, _key: str, _text: str) -> None:
        raise AssertionError("EntityExtractor must not write FileIndex LLM cache")


def _response(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="test-model", prompt_hash="provider")


def _valid_entity_text() -> str:
    return (
        f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型'
        f"{TUPLE_DELIMITER}一款车型){COMPLETION_DELIMITER}"
    )


@pytest.mark.unit
class TestEntityExtractorGateway:
    async def test_initial_and_gleaning_calls_use_strong_rich_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            if request.purpose == "entity_relation_extract":
                return _response(_valid_entity_text())
            if request.purpose == "entity_relation_gleaning":
                return _response(COMPLETION_DELIMITER)
            raise AssertionError(request.purpose)

        monkeypatch.setattr(extractor_module, "execute_llm", capture)
        extractor = EntityExtractor(
            SimpleNamespace(strong_llm=_Adapter()),  # type: ignore[arg-type]
            prompt_template=(
                "抽取 {entity_types} {tuple_delimiter} {record_delimiter} "
                "{completion_delimiter} {input_text}"
            ),
            gleaning_rounds=1,
            file_index=_ForbiddenFileIndex(),  # type: ignore[arg-type]
        )

        result = await extractor.extract_from_chunk(
            11,
            "客户咨询 CS75 Plus",
            recording_id=7,
            tenant_id="tenant-a",
        )

        assert result.entities
        assert [request.purpose for request in captured] == [
            "entity_relation_extract",
            "entity_relation_gleaning",
        ]
        for request in captured:
            assert request.tenant_id == "tenant-a"
            assert request.model_tier == "strong"
            assert request.cache_policy is CachePolicy.EXACT
            assert request.ttl_seconds == 90 * 24 * 60 * 60
            assert request.messages
            assert request.prompt_version
            assert request.schema_version
            assert request.parser_version
            assert request.postprocessor_version
            assert request.business_snapshot
            assert request.response_validator is not None
            assert any(
                getattr(ref, "source_type", None) == "recording"
                and getattr(ref, "source_id", None) == "7"
                for ref in request.provenance
            )
            assert any(
                getattr(ref, "source_type", None) == "chunk"
                and getattr(ref, "source_id", None) == "11"
                for ref in request.provenance
            )
        assert captured[0].response_validator is not None
        assert captured[0].response_validator(_response(_valid_entity_text()))
        assert not captured[0].response_validator(_response("not structured"))
        assert captured[1].response_validator is not None
        assert captured[1].response_validator(_response(COMPLETION_DELIMITER))

    async def test_gleaning_does_not_retry_all_exceptions_locally(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        purposes: list[str] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            purposes.append(request.purpose)
            if request.purpose == "entity_relation_extract":
                return _response(_valid_entity_text())
            raise RuntimeError("non-transient parser/provider failure")

        monkeypatch.setattr(extractor_module, "execute_llm", capture)
        extractor = EntityExtractor(
            SimpleNamespace(strong_llm=_Adapter()),  # type: ignore[arg-type]
            prompt_template=(
                "抽取 {entity_types} {tuple_delimiter} {record_delimiter} "
                "{completion_delimiter} {input_text}"
            ),
            gleaning_rounds=1,
            max_gleaning_retry=9,
        )

        result = await extractor.extract_from_chunk(
            11,
            "客户咨询 CS75 Plus",
            recording_id=7,
            tenant_id="tenant-a",
        )

        assert result.entities
        assert purposes == ["entity_relation_extract", "entity_relation_gleaning"]


@pytest.mark.unit
class TestEvalJudgeGateway:
    async def test_three_methods_use_tenant_scoped_rich_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[LLMRequest] = []

        async def capture(_adapter: Any, request: LLMRequest) -> LLMResponse:
            captured.append(request)
            responses = {
                "extract_facts": "- fact one\n- fact two",
                "judge_faithfulness": (
                    '{"id": 1, "supported": true}\n{"id": 2, "supported": false}'
                ),
                "judge_relevance": "1.0",
            }
            return _response(responses[request.purpose])

        monkeypatch.setattr(judge_module, "execute_llm", capture)
        judge = LLMJudge(
            llm=_Adapter(),  # type: ignore[arg-type]
            tenant_id="tenant-eval",
            dataset_id="gold-v3",
        )

        assert await judge.extract_facts("answer", example_id="example-9") == [
            "fact one",
            "fact two",
        ]
        assert await judge.judge_faithfulness(
            "context",
            ["fact one", "fact two"],
            example_id="example-9",
        ) == [True, False]
        assert (
            await judge.judge_relevance(
                "query",
                "answer",
                example_id="example-9",
            )
            == 1.0
        )

        assert [request.purpose for request in captured] == [
            "extract_facts",
            "judge_faithfulness",
            "judge_relevance",
        ]
        for request in captured:
            assert request.tenant_id == "tenant-eval"
            assert request.model_tier == "strong"
            assert request.cache_policy is CachePolicy.EXACT
            assert request.ttl_seconds == 90 * 24 * 60 * 60
            assert request.response_validator is not None
            assert any(
                getattr(ref, "source_type", None) == "eval_dataset"
                and getattr(ref, "source_id", None) == "gold-v3"
                for ref in request.provenance
            )
            assert any(
                getattr(ref, "source_type", None) == "eval_example"
                and getattr(ref, "source_id", None) == "example-9"
                for ref in request.provenance
            )
        assert captured[1].response_validator is not None
        assert not captured[1].response_validator(_response('{"id": 1, "supported": true}'))
        assert captured[2].response_validator is not None
        assert not captured[2].response_validator(_response("0.7"))
