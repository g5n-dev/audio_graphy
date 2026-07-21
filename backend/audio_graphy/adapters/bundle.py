"""Adapter bundle — a container that holds all 6 adapters wired together."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from audio_graphy.adapters.protocols import (
    ASRAdapter,
    AudioEmbedAdapter,
    EmbedAdapter,
    LLMAdapter,
    VADAdapter,
    VoiceprintAdapter,
)

if TYPE_CHECKING:
    from audio_graphy.config import Settings


@dataclass(frozen=True, slots=True)
class AdapterBundle:
    """Aggregate of all model adapters.

    Modules depend on this bundle rather than individual adapters — keeps
    DI simple and lets ``ADAPTER_*_MODE`` swap the whole bundle at startup.

    M7 adds two optional adapters: ``audio_embed`` (CLAP) and ``voiceprint``
    (CAM++). They are ``None`` when ``enable_clap`` / ``enable_voiceprint``
    feature flags are off, guaranteeing zero side-effects for M3–M6 callers.
    """

    vad: VADAdapter
    asr: ASRAdapter
    strong_llm: LLMAdapter  # entity extraction / final answer / segment filter
    weak_llm: LLMAdapter  # query rewrite / summary / keywords / tag judgment
    embed: EmbedAdapter
    audio_embed: AudioEmbedAdapter | None = None  # M7
    voiceprint: VoiceprintAdapter | None = None  # M7


def build_mock_bundle(settings: Settings) -> AdapterBundle:
    """Construct a fully-mocked bundle (default for all-mock mode).

    M7: audio_embed / voiceprint are still gated by the ``enable_clap`` /
    ``enable_voiceprint`` feature flags so the bundle shape is consistent
    between mock / hybrid paths. When enabled, mock adapters are wired in
    (real adapters never run in mock bundle).
    """
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_audio_embed import MockAudioEmbedAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter
    from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter

    audio_embed: AudioEmbedAdapter | None = None
    if settings.enable_clap:
        audio_embed = MockAudioEmbedAdapter()

    voiceprint: VoiceprintAdapter | None = None
    if settings.enable_voiceprint:
        voiceprint = MockVoiceprintAdapter()

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
        audio_embed=audio_embed,
        voiceprint=voiceprint,
    )


def build_hybrid_bundle(settings: Settings) -> AdapterBundle:
    """Build a bundle where each adapter independently picks mock/real by its mode field.

    构造混合 bundle：每个 adapter 独立根据自身 mode 字段选择 mock/real 实现。

    M5: ASR real mode is now supported via ``FunASRAdapter`` (OpenAI-compat API).
    M7: ``audio_embed`` (CLAP) and ``voiceprint`` (CAM++) added. Both are
        gated by feature flags ``enable_clap`` / ``enable_voiceprint`` in
        addition to their per-adapter mode fields. When the flag is off,
        the corresponding adapter is ``None`` (zero side-effects).

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

    # Embedding (text)
    if settings.adapter_embed_mode == "real":
        from audio_graphy.adapters.real.embed_bge import BGEEmbedAdapter

        embed: EmbedAdapter = BGEEmbedAdapter(
            url=settings.bge_m3_url,
            model="bge-m3",
            dim=settings.embedding_dim,
        )
    else:
        embed = MockEmbedAdapter(dim=settings.embedding_dim)

    # M7 — audio_embed (CLAP). Gated by enable_clap flag.
    audio_embed: AudioEmbedAdapter | None = None
    if settings.enable_clap:
        if settings.adapter_audio_embed_mode == "real":
            from audio_graphy.adapters.real.audio_embed_clap import CLAPServiceAdapter

            audio_embed = CLAPServiceAdapter(url=settings.clap_service_url)
        else:
            from audio_graphy.adapters.mock_audio_embed import MockAudioEmbedAdapter

            audio_embed = MockAudioEmbedAdapter()

    # M7 — voiceprint (CAM++). Gated by enable_voiceprint flag.
    voiceprint: VoiceprintAdapter | None = None
    if settings.enable_voiceprint:
        if settings.adapter_voiceprint_mode == "real":
            from audio_graphy.adapters.real.voiceprint_cam import CAMPlusPlusAdapter

            voiceprint = CAMPlusPlusAdapter(url=settings.campplus_service_url)
        else:
            from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter

            voiceprint = MockVoiceprintAdapter()

    return AdapterBundle(
        vad=vad,
        asr=asr,
        strong_llm=strong_llm,
        weak_llm=weak_llm,
        embed=embed,
        audio_embed=audio_embed,
        voiceprint=voiceprint,
    )
