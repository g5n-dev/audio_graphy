"""Unit tests for MockLLMAdapter — deterministic LLM responses + cache."""

from __future__ import annotations

import pytest

from audio_graphy.adapters.mock_llm import MockLLMAdapter
from audio_graphy.adapters.protocols import LLMResponse


@pytest.fixture
def adapter() -> MockLLMAdapter:
    return MockLLMAdapter(model="test-model", error_rate=0.0, latency_ms=0)


@pytest.fixture
def simple_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, who are you?"},
    ]


class TestMockLLMComplete:
    """MockLLMAdapter.complete() behavior."""

    def test_exposes_provider_and_model_epoch(self) -> None:
        adapter = MockLLMAdapter(
            model="served-model",
            model_epoch="fixture-v3",
            error_rate=0.0,
            latency_ms=0,
        )
        assert adapter.provider == "mock"
        assert adapter.model_epoch == "fixture-v3"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_llm_response(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        result = await adapter.complete(simple_messages)
        assert isinstance(result, LLMResponse)
        assert result.text
        assert result.model == "test-model"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deterministic_same_messages_same_text(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        first = await adapter.complete(simple_messages)
        second = await adapter.complete(simple_messages)
        # Same messages → same hash → same bucket → same text
        assert first.text == second.text
        assert first.prompt_hash == second.prompt_hash

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prompt_hash_is_sha256(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        result = await adapter.complete(simple_messages)
        assert len(result.prompt_hash) == 64
        assert all(c in "0123456789abcdef" for c in result.prompt_hash)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_different_messages_different_hash(self, adapter: MockLLMAdapter) -> None:
        msgs_a = [{"role": "user", "content": "Hello"}]
        msgs_b = [{"role": "user", "content": "World"}]
        r_a = await adapter.complete(msgs_a)
        r_b = await adapter.complete(msgs_b)
        assert r_a.prompt_hash != r_b.prompt_hash

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_usage_tokens_populated(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        result = await adapter.complete(simple_messages)
        assert "prompt_tokens" in result.usage
        assert "completion_tokens" in result.usage
        assert "total_tokens" in result.usage
        assert result.usage["total_tokens"] == (
            result.usage["prompt_tokens"] + result.usage["completion_tokens"]
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_cache_key_returns_cached_true_on_second_call(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        cache_key = "my-cache-key"
        first = await adapter.complete(simple_messages, cache_key=cache_key)
        assert first.cached is False

        second = await adapter.complete(simple_messages, cache_key=cache_key)
        assert second.cached is True
        assert second.text == first.text

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_different_cache_keys_yield_uncached(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        first = await adapter.complete(simple_messages, cache_key="key-1")
        second = await adapter.complete(simple_messages, cache_key="key-2")
        assert first.cached is False
        assert second.cached is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_cache_key_never_cached(
        self, adapter: MockLLMAdapter, simple_messages: list[dict[str, str]]
    ) -> None:
        first = await adapter.complete(simple_messages)
        second = await adapter.complete(simple_messages)
        assert first.cached is False
        assert second.cached is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_error_rate_zero_never_raises(
        self, simple_messages: list[dict[str, str]]
    ) -> None:
        adapter = MockLLMAdapter(model="t", error_rate=0.0, latency_ms=0)
        for _ in range(100):
            result = await adapter.complete(simple_messages)
            assert isinstance(result, LLMResponse)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_error_rate_one_always_raises(
        self, simple_messages: list[dict[str, str]]
    ) -> None:
        adapter = MockLLMAdapter(model="t", error_rate=1.0, latency_ms=0)
        with pytest.raises(RuntimeError, match="Mock LLM simulated error"):
            await adapter.complete(simple_messages)

    @pytest.mark.unit
    def test_compute_prompt_hash_static(self, simple_messages: list[dict[str, str]]) -> None:
        """Static method can be called without instance."""
        h1 = MockLLMAdapter.compute_prompt_hash("model-a", simple_messages)
        h2 = MockLLMAdapter.compute_prompt_hash("model-a", simple_messages)
        h3 = MockLLMAdapter.compute_prompt_hash("model-b", simple_messages)
        assert len(h1) == 64
        assert h1 == h2
        assert h1 != h3

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_legacy_tag_batch_prompt_returns_complete_structured_json_once(
        self,
        adapter: MockLLMAdapter,
    ) -> None:
        import json

        tag_paths = [
            "quality.greeting",
            "quality.closing",
            "sales.product_mention",
        ]
        messages = [
            {
                "role": "system",
                "content": "你是门店接待质检分类器。必须仅返回 JSON。",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "recording_id": 7,
                        "tag_paths": tag_paths,
                        "transcript": "您好，欢迎光临。",
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        result = await adapter.complete(messages, temperature=0.0, max_tokens=512)
        payload = json.loads(result.text)

        assert adapter.call_count == 1
        assert [row["tag_path"] for row in payload["tags"]] == tag_paths
        assert {row["value"] for row in payload["tags"]} <= {"pass", "fail"}
        assert {row["confidence"] for row in payload["tags"]} == {0.95}
