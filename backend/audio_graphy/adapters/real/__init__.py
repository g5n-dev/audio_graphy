"""Real adapter subpackage — production-grade HTTP-backed adapters.

真实 Adapter 子包：vLLM (OpenAI-compatible) / Silero VAD / HuggingFace TEI (bge-m3) / funASR.

Public API:
    from audio_graphy.adapters.real import (
        BGEEmbedAdapter,
        FunASRAdapter,
        LLMOpenAIAdapter,
        SileroVADAdapter,
    )
"""

from __future__ import annotations

from audio_graphy.adapters.real.embed_bge import BGEEmbedAdapter
from audio_graphy.adapters.real.funasr import FunASRAdapter
from audio_graphy.adapters.real.llm_openai import LLMOpenAIAdapter
from audio_graphy.adapters.real.vad_silero import SileroVADAdapter

__all__ = ["BGEEmbedAdapter", "FunASRAdapter", "LLMOpenAIAdapter", "SileroVADAdapter"]
