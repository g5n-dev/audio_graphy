"""Unit tests for MockEmbedAdapter — deterministic hash-based vectors."""

from __future__ import annotations

import math

import pytest

from audio_graphy.adapters.mock_embed import MockEmbedAdapter
from audio_graphy.adapters.protocols import EmbeddingResult


@pytest.fixture
def adapter() -> MockEmbedAdapter:
    return MockEmbedAdapter(dim=1024, model="mock-bge", latency_ms=0)


class TestMockEmbedTexts:
    """MockEmbedAdapter.embed_texts() behavior."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_returns_one_result_per_input(self, adapter: MockEmbedAdapter) -> None:
        texts = ["hello", "world", "foo"]
        results = await adapter.embed_texts(texts)
        assert len(results) == 3
        assert all(isinstance(r, EmbeddingResult) for r in results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_vector_dim_matches_config(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts(["test"])
        assert results[0].dim == 1024
        assert len(results[0].vector) == 1024

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deterministic_same_text_same_vector(self, adapter: MockEmbedAdapter) -> None:
        results_a = await adapter.embed_texts(["foo"])
        results_b = await adapter.embed_texts(["foo"])
        assert results_a[0].vector == results_b[0].vector

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_different_texts_different_vectors(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts(["foo", "bar"])
        assert results[0].vector != results[1].vector

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_vector_is_l2_normalized(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts(["any text"])
        v = results[0].vector
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6, f"L2 norm should be 1.0, got {norm}"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_text_returns_valid_vector(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts([""])
        assert len(results) == 1
        assert len(results[0].vector) == 1024

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_list(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts([])
        assert len(results) == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_chinese_text_works(self, adapter: MockEmbedAdapter) -> None:
        results = await adapter.embed_texts(["你好世界", "门店录音图谱"])
        assert len(results) == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_model_attribute_set(self, adapter: MockEmbedAdapter) -> None:
        assert adapter.model == "mock-bge"
        results = await adapter.embed_texts(["test"])
        assert results[0].model == "mock-bge"


class TestMockEmbedValidation:
    """Constructor validation."""

    @pytest.mark.unit
    def test_zero_dim_rejected(self) -> None:
        with pytest.raises(ValueError, match="dim must be positive"):
            MockEmbedAdapter(dim=0)

    @pytest.mark.unit
    def test_non_multiple_of_8_rejected(self) -> None:
        with pytest.raises(ValueError, match="dim must be positive multiple of 8"):
            MockEmbedAdapter(dim=100)

    @pytest.mark.unit
    def test_valid_dim_accepted(self) -> None:
        MockEmbedAdapter(dim=8)
        MockEmbedAdapter(dim=512)
        MockEmbedAdapter(dim=1024)
        MockEmbedAdapter(dim=2048)
