"""Adapter bundle — a container that holds all 4 adapters wired together."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from audio_graphy.adapters.protocols import (
    ASRAdapter,
    EmbedAdapter,
    LLMAdapter,
    VADAdapter,
)

if TYPE_CHECKING:
    from audio_graphy.config import Settings


@dataclass(frozen=True, slots=True)
class AdapterBundle:
    """Aggregate of all model adapters.

    Modules depend on this bundle rather than individual adapters — keeps
    DI simple and lets `ADAPTER_MODE` swap the whole bundle at startup.
    """

    vad: VADAdapter
    asr: ASRAdapter
    strong_llm: LLMAdapter  # entity extraction / final answer / segment filter
    weak_llm: LLMAdapter  # query rewrite / summary / keywords / tag judgment
    embed: EmbedAdapter


def build_mock_bundle(settings: Settings) -> AdapterBundle:
    """Construct a fully-mocked bundle (default for ADAPTER_MODE=mock)."""
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter

    return AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(flaky=settings.mock_asr_flaky),
        strong_llm=MockLLMAdapter(
            model=settings.llm_strong_model,
            error_rate=settings.mock_llm_error_rate,
        ),
        weak_llm=MockLLMAdapter(
            model=settings.llm_weak_model,
            error_rate=settings.mock_llm_error_rate,
        ),
        embed=MockEmbedAdapter(dim=settings.embedding_dim),
    )
