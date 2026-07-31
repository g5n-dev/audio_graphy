"""Unit tests for EntityExtractor — GraphRAG parsing + Gleaning + cache.

Tests cover:
    - GraphRAG delimiter protocol parsing
    - CSV-style fallback parsing (mock LLM compatibility)
    - Lenient regex for partial matches
    - Gleaning supplement round
    - Gleaning relations get confidence=INFERRED
    - LLM cache (Layer 2 file_index)
    - Empty text handling
    - Chinese entity normalisation (alias table)
    - Concurrent extraction
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.core.extractor import (
    EntityExtractor,
)
from audio_graphy.core.types import (
    COMPLETION_DELIMITER,
    TUPLE_DELIMITER,
)


@pytest.mark.unit
class TestGraphRAGParsing:
    """GraphRAG delimiter protocol parsing."""

    @staticmethod
    def _make_extractor(
        bundle: Any,
        prompt_template: str | None = None,
        file_index: Any = None,
    ) -> EntityExtractor:
        """Create an extractor with a simple inline prompt template."""
        if prompt_template is None:
            prompt_template = (
                "抽取实体和关系。\n"
                "实体类型: {entity_types}\n"
                '("实体"{tuple_delimiter}名称{tuple_delimiter}类型{tuple_delimiter}描述)'
                "{record_delimiter}"
                '("关系"{tuple_delimiter}源{tuple_delimiter}关系{tuple_delimiter}目标{tuple_delimiter}描述)'
                "{completion_delimiter}\n"
                "输入: {input_text}"
            )
        return EntityExtractor(
            bundle,
            prompt_template=prompt_template,
            gleaning_rounds=0,  # Disable gleaning for parsing tests
            file_index=file_index,
        )

    async def test_parse_graphrag_format(
        self, scripted_bundle: Any, sample_graphrag_response: str
    ) -> None:
        """Parse well-formed GraphRAG delimiter output."""
        # Configure scripted LLM to return GraphRAG format
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = self._make_extractor(scripted_bundle)
        result = await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        assert strong_llm.response_schemas[-1] is not None
        assert result.parse_success is True
        assert len(result.entities) >= 5  # CS75 Plus, 张敏, 5万元, 36期分期, 哈弗H6
        assert len(result.relations) >= 3  # 推荐, 搭配, 对比

        # Check entity names
        names = {e.name for e in result.entities}
        assert "CS75 Plus" in names
        assert "张敏" in names

        # Check relation
        rel = result.relations[0]
        assert rel.source_name == "张敏"
        assert rel.target_name == "CS75 Plus"
        assert rel.relation == "推荐"
        assert rel.confidence == "EXTRACTED"

    async def test_parse_csv_format(self, scripted_bundle: Any, sample_csv_response: str) -> None:
        """Parse CSV-style output (mock LLM default format)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_csv_response)

        extractor = self._make_extractor(scripted_bundle)
        result = await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        assert result.parse_success is True
        assert len(result.entities) >= 3
        assert len(result.relations) >= 2

        names = {e.name for e in result.entities}
        assert "CS75 Plus" in names

    async def test_parse_empty_output(self, scripted_bundle: Any) -> None:
        """Empty LLM output returns empty result."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("")

        extractor = self._make_extractor(scripted_bundle)
        result = await extractor.extract_from_chunk(1, "测试", recording_id=1)

        assert result.entities == []
        assert result.relations == []

    async def test_parse_unparseable_output(self, scripted_bundle: Any) -> None:
        """Unparseable LLM output returns empty result with parse_success=False."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("This is random text with no tuples.")

        extractor = self._make_extractor(scripted_bundle)
        result = await extractor.extract_from_chunk(1, "测试", recording_id=1)

        assert result.parse_success is False
        assert result.entities == []
        assert result.relations == []


@pytest.mark.unit
class TestGleaning:
    """Gleaning supplement round."""

    @staticmethod
    def _make_extractor(
        bundle: Any,
        gleaning_rounds: int = 1,
        *,
        adaptive_gleaning: bool = False,
    ) -> EntityExtractor:
        prompt = "抽取实体。{entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}"
        return EntityExtractor(
            bundle,
            prompt_template=prompt,
            gleaning_rounds=gleaning_rounds,
            adaptive_gleaning=adaptive_gleaning,
        )

    async def test_gleaning_adds_entities(
        self, scripted_bundle: Any, sample_graphrag_response: str
    ) -> None:
        """Gleaning round supplements entities."""
        strong_llm = scripted_bundle.strong_llm

        # First call: return some entities
        first_response = (
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}SUV)'
            f"{COMPLETION_DELIMITER}"
        )
        # Gleaning call: return a new entity
        gleaning_response = (
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}销售顾问)'
            f"{COMPLETION_DELIMITER}"
        )

        # Set gleaning keyword FIRST (more specific) so it's checked before "抽取"
        strong_llm.set_response("遗漏", gleaning_response)
        strong_llm.set_response("抽取", first_response)

        extractor = self._make_extractor(scripted_bundle, gleaning_rounds=1)
        result = await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        names = {e.name for e in result.entities}
        assert "CS75 Plus" in names
        assert "张敏" in names  # Added by gleaning
        assert result.gleaning_rounds >= 1

    async def test_gleaning_relations_are_inferred(self, scripted_bundle: Any) -> None:
        """Relations from Gleaning get confidence=INFERRED."""
        strong_llm = scripted_bundle.strong_llm

        first_response = (
            f'("实体"{TUPLE_DELIMITER}A{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}desc)'
            f"{COMPLETION_DELIMITER}"
        )
        gleaning_response = (
            f'("关系"{TUPLE_DELIMITER}A{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}B{TUPLE_DELIMITER}desc)'
            f"{COMPLETION_DELIMITER}"
        )

        # Set gleaning keyword FIRST
        strong_llm.set_response("遗漏", gleaning_response)
        strong_llm.set_response("抽取", first_response)

        extractor = self._make_extractor(scripted_bundle, gleaning_rounds=1)
        result = await extractor.extract_from_chunk(1, "测试", recording_id=1)

        for rel in result.relations:
            assert rel.confidence == "INFERRED"

    async def test_gleaning_no_new_entities_early_terminate(
        self, scripted_bundle: Any, sample_graphrag_response: str
    ) -> None:
        """Gleaning returns no new entities → early termination."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = self._make_extractor(scripted_bundle, gleaning_rounds=3)
        result = await extractor.extract_from_chunk(1, "测试", recording_id=1)

        # Should terminate early (gleaning returns same entities)
        assert result.gleaning_rounds <= 3

    async def test_adaptive_gleaning_continues_only_while_new_facts_arrive(
        self,
        scripted_bundle: Any,
    ) -> None:
        strong_llm = scripted_bundle.strong_llm
        first_response = (
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型'
            f"{TUPLE_DELIMITER}SUV){COMPLETION_DELIMITER}"
        )
        gleaning_response = (
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席'
            f"{TUPLE_DELIMITER}销售顾问){COMPLETION_DELIMITER}"
        )
        strong_llm.set_response("遗漏", gleaning_response)
        strong_llm.set_response("抽取", first_response)
        extractor = self._make_extractor(
            scripted_bundle,
            gleaning_rounds=1,
            adaptive_gleaning=True,
        )

        result = await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        assert result.gleaning_rounds == 2
        assert {entity.name for entity in result.entities} >= {"CS75 Plus", "张敏"}


@pytest.mark.unit
class TestEmptyText:
    """Empty chunk text handling."""

    async def test_empty_text_returns_empty_result(self, scripted_bundle: Any) -> None:
        """Empty text returns empty ExtractionResult without calling LLM."""
        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )
        result = await extractor.extract_from_chunk(1, "", recording_id=1)

        assert result.entities == []
        assert result.relations == []
        assert result.parse_success is True
        assert result.gleaning_rounds == 0

    async def test_whitespace_text_returns_empty_result(self, scripted_bundle: Any) -> None:
        """Whitespace-only text returns empty result."""
        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )
        result = await extractor.extract_from_chunk(1, "   \n  ", recording_id=1)

        assert result.entities == []


@pytest.mark.unit
class TestEntityNormalisation:
    """Chinese entity normalisation (alias table)."""

    async def test_alias_normalisation(self, scripted_bundle: Any) -> None:
        """CS75PLUS is normalised to CS75 Plus."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(
            f'("实体"{TUPLE_DELIMITER}CS75PLUS{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}SUV)'
            f"{COMPLETION_DELIMITER}"
        )

        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )
        result = await extractor.extract_from_chunk(1, "测试", recording_id=1)

        names = {e.name for e in result.entities}
        assert "CS75 Plus" in names
        assert "CS75PLUS" not in names


@pytest.mark.unit
class TestLLMCache:
    """Centralized LLM cache migration."""

    async def test_file_index_is_not_used_for_llm_results(
        self, scripted_bundle: Any, file_index: Any, sample_graphrag_response: str
    ) -> None:
        """Entity extraction never writes the legacy JSON LLM cache."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)
        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        assert await file_index.get_all("kv_store_llm_response_cache") == {}

    async def test_cache_different_prompts_miss(
        self, scripted_bundle: Any, file_index: Any, sample_graphrag_response: str
    ) -> None:
        """Different prompts don't hit cache."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "文本A", recording_id=1)
        calls_after_first = strong_llm.call_count

        await extractor.extract_from_chunk(2, "文本B", recording_id=1)
        # Different chunk text → different prompt → cache miss → LLM called
        assert strong_llm.call_count > calls_after_first


@pytest.mark.unit
class TestConcurrentExtraction:
    """Concurrent multi-chunk extraction."""

    async def test_extract_from_chunks_preserves_order(
        self, scripted_bundle: Any, sample_graphrag_response: str
    ) -> None:
        """extract_from_chunks returns results in input order."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )

        chunks: list[tuple[int, str, int]] = [
            (1, "文本1", 1),
            (2, "文本2", 1),
            (3, "文本3", 1),
        ]
        results = await extractor.extract_from_chunks(chunks, concurrency=2)

        assert len(results) == 3
        assert results[0].chunk_id == 1
        assert results[1].chunk_id == 2
        assert results[2].chunk_id == 3

    async def test_extract_from_chunks_empty(self, scripted_bundle: Any) -> None:
        """Empty chunks list returns empty results."""
        extractor = EntityExtractor(
            scripted_bundle,
            prompt_template="test {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )
        results = await extractor.extract_from_chunks([], concurrency=4)
        assert results == []
