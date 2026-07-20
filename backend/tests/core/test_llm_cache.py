"""LLM cache tests (AC-23, AC-24) — dual-layer cache hit/miss.

Tests verify:
    AC-23: Same prompt re-run → cache hit (cached=True, call_count doesn't increase)
    AC-24: Cache persistence: flush → reload → re-run → cache hit (file_index Layer 2)
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.core.extractor import EntityExtractor


@pytest.mark.unit
class TestLLMCacheHit:
    """AC-23: Same prompt re-run hits cache."""

    async def test_adapter_cache_hit(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Second extraction with same text → adapter cache hit (call_count unchanged)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        # First call: cache miss
        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)
        calls_after_first = strong_llm.call_count

        # Second call: same text → same prompt → cache hit
        await extractor.extract_from_chunk(1, "测试文本", recording_id=1)

        # AC-23: call_count should NOT increase (cache hit)
        assert strong_llm.call_count == calls_after_first

    async def test_file_index_cache_hit(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """file_index Layer 2 cache hit (no adapter call)."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )

        # First call: populates file_index cache
        await extractor.extract_from_chunk(1, "缓存测试", recording_id=1)
        await file_index.flush()

        # Clear adapter cache to force Layer 2 lookup
        strong_llm._cache.clear()
        calls_before = strong_llm.call_count

        # Second call: should hit file_index cache (no adapter call)
        await extractor.extract_from_chunk(1, "缓存测试", recording_id=1)

        # call_count should NOT increase (file_index cache hit)
        assert strong_llm.call_count == calls_before


@pytest.mark.integration
class TestLLMCachePersistence:
    """AC-24: Cache persists across flush → reload."""

    async def test_cache_survives_flush_reload(
        self,
        scripted_bundle: Any,
        file_index: Any,
        sample_graphrag_response: str,
    ) -> None:
        """Cache survives: extract → flush → reload → re-extract → cache hit."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        # First extractor: extracts and caches
        extractor1 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        await extractor1.extract_from_chunk(1, "持久化缓存测试", recording_id=1)
        await file_index.flush()  # Persist to disk
        calls_after_first = strong_llm.call_count

        # Clear adapter cache (simulates new process)
        strong_llm._cache.clear()

        # Second extractor: same file_index, reload from disk
        await file_index.load()  # Reload from disk

        extractor2 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        await extractor2.extract_from_chunk(1, "持久化缓存测试", recording_id=1)

        # AC-24: Should hit file_index cache — no new LLM call
        assert strong_llm.call_count == calls_after_first

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
