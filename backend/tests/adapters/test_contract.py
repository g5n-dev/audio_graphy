"""Contract tests — verify each mock adapter satisfies its Protocol.

These tests are cheap and run before any unit/integration test.
If a mock fails to satisfy the Protocol, every downstream test is meaningless.
"""

from __future__ import annotations

import pytest

from audio_graphy.adapters import (
    ASRAdapter,
    EmbedAdapter,
    LLMAdapter,
    VADAdapter,
)
from audio_graphy.adapters.mock_asr import MockASRAdapter
from audio_graphy.adapters.mock_embed import MockEmbedAdapter
from audio_graphy.adapters.mock_llm import MockLLMAdapter
from audio_graphy.adapters.mock_vad import MockVADAdapter


class TestVADAdapterContract:
    """MockVADAdapter must satisfy the VADAdapter Protocol."""

    @pytest.mark.contract
    def test_is_vad_adapter(self) -> None:
        adapter = MockVADAdapter()
        assert isinstance(adapter, VADAdapter)

    @pytest.mark.contract
    def test_has_segment_method(self) -> None:
        adapter = MockVADAdapter()
        assert callable(getattr(adapter, "segment", None))


class TestASRAdapterContract:
    """MockASRAdapter must satisfy the ASRAdapter Protocol."""

    @pytest.mark.contract
    def test_is_asr_adapter(self) -> None:
        adapter = MockASRAdapter()
        assert isinstance(adapter, ASRAdapter)

    @pytest.mark.contract
    def test_has_transcribe_method(self) -> None:
        adapter = MockASRAdapter()
        assert callable(getattr(adapter, "transcribe", None))


class TestLLMAdapterContract:
    """MockLLMAdapter must satisfy the LLMAdapter Protocol."""

    @pytest.mark.contract
    def test_is_llm_adapter(self) -> None:
        adapter = MockLLMAdapter(model="test-model")
        assert isinstance(adapter, LLMAdapter)

    @pytest.mark.contract
    def test_has_model_attribute(self) -> None:
        adapter = MockLLMAdapter(model="test-model")
        assert adapter.model == "test-model"

    @pytest.mark.contract
    def test_has_complete_method(self) -> None:
        adapter = MockLLMAdapter(model="test-model")
        assert callable(getattr(adapter, "complete", None))


class TestEmbedAdapterContract:
    """MockEmbedAdapter must satisfy the EmbedAdapter Protocol."""

    @pytest.mark.contract
    def test_is_embed_adapter(self) -> None:
        adapter = MockEmbedAdapter(dim=1024)
        assert isinstance(adapter, EmbedAdapter)

    @pytest.mark.contract
    def test_has_model_and_dim(self) -> None:
        adapter = MockEmbedAdapter(dim=1024, model="mock-bge")
        assert adapter.model == "mock-bge"
        assert adapter.dim == 1024

    @pytest.mark.contract
    def test_has_embed_texts_method(self) -> None:
        adapter = MockEmbedAdapter(dim=1024)
        assert callable(getattr(adapter, "embed_texts", None))
