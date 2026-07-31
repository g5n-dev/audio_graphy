"""EntityExtractor cache-boundary regression tests.

The centralized gateway owns persistence. EntityExtractor must not populate
the legacy FileIndex JSON LLM cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.core.extractor import EntityExtractor


@pytest.mark.unit
class TestLLMCacheHit:
    """Canonical gateway recipes and legacy-cache removal."""

    async def test_same_recipe_uses_one_adapter_cache_identity(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Compatibility adapters receive the same canonical recipe SHA."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)
        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        assert len(strong_llm._cache) == 1

    async def test_file_index_cache_remains_empty(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Gateway migration never writes FileIndex LLM cache."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "缓存测试", recording_id=1)
        await file_index.flush()

        assert await file_index.get_all("kv_store_llm_response_cache") == {}


@pytest.mark.integration
class TestLLMCachePersistence:
    """FileIndex no longer persists model outputs."""

    async def test_flush_reload_does_not_create_legacy_llm_cache(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """A flush/reload cycle leaves the old JSON cache empty."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor1 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        await extractor1.extract_from_chunk(1, "持久化缓存测试", recording_id=1)
        await file_index.flush()
        await file_index.load()

        assert await file_index.get_all("kv_store_llm_response_cache") == {}

    async def test_different_prompts_no_cache_hit(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Different prompts don't hit cache."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        await extractor.extract_from_chunk(1, "文本A", recording_id=1)
        calls_after_a = strong_llm.call_count

        await extractor.extract_from_chunk(2, "文本B", recording_id=1)
        # Different chunk text → different prompt → cache miss
        assert strong_llm.call_count > calls_after_a
