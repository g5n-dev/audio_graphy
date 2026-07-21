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
    DI simple and lets `ADAPTER_*_MODE` swap the whole bundle at startup.
    """

    vad: VADAdapter
    asr: ASRAdapter
    strong_llm: LLMAdapter  # entity extraction / final answer / segment filter
    weak_llm: LLMAdapter  # query rewrite / summary / keywords / tag judgment
    embed: EmbedAdapter


def build_mock_bundle(settings: Settings) -> AdapterBundle:
    """Construct a fully-mocked bundle (default for all-mock mode)."""
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


def build_hybrid_bundle(settings: Settings) -> AdapterBundle:
    """Build a bundle where each adapter independently picks mock/real by its mode field.

    构造混合 bundle：每个 adapter 独立根据自身 mode 字段选择 mock/real 实现。

    M5: ASR real mode is now supported via ``FunASRAdapter`` (OpenAI-compat API).

    Callers SHOULD ensure ``aclose()`` is invoked on the returned adapters at
    application shutdown — real adapters own httpx pools; mock adapters are
    no-op. See ``adapters/real/*`` for lifecycle details.
    """
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter

    # VAD
    if settings.adapter_vad_mode == "real":
        from audio_graphy.adapters.real.vad_silero import SileroVADAdapter

        vad: VADAdapter = SileroVADAdapter(url=settings.silero_vad_url)
    else:
        vad = MockVADAdapter()

    # ASR — M5 unblocks real mode via FunASRAdapter.
    if settings.adapter_asr_mode == "real":
        from audio_graphy.adapters.real.funasr import FunASRAdapter

        asr: ASRAdapter = FunASRAdapter(
            url=settings.funasr_url,
            model=settings.funasr_model,
            api_key=settings.openai_api_key,
            language="zh",
        )
    else:
        asr = MockASRAdapter(flaky=settings.mock_asr_flaky)

    # LLM strong/weak share mode; differ in base_url + model
    if settings.adapter_llm_mode == "real":
        from audio_graphy.adapters.real.llm_openai import LLMOpenAIAdapter

        strong_llm: LLMAdapter = LLMOpenAIAdapter(
            base_url=settings.openai_base_url_strong,
            api_key=settings.openai_api_key,
            model=settings.llm_strong_model,
        )
        weak_llm: LLMAdapter = LLMOpenAIAdapter(
            base_url=settings.openai_base_url_weak,
            api_key=settings.openai_api_key,
            model=settings.llm_weak_model,
        )
    else:
        strong_llm = MockLLMAdapter(
            model=settings.llm_strong_model, error_rate=settings.mock_llm_error_rate,
        )
        weak_llm = MockLLMAdapter(
            model=settings.llm_weak_model, error_rate=settings.mock_llm_error_rate,
        )

    # Embedding
    if settings.adapter_embed_mode == "real":
        from audio_graphy.adapters.real.embed_bge import BGEEmbedAdapter

        embed: EmbedAdapter = BGEEmbedAdapter(
            url=settings.bge_m3_url,
            model="bge-m3",
            dim=settings.embedding_dim,
        )
    else:
        embed = MockEmbedAdapter(dim=settings.embedding_dim)

    return AdapterBundle(
        vad=vad, asr=asr, strong_llm=strong_llm, weak_llm=weak_llm, embed=embed,
    )
