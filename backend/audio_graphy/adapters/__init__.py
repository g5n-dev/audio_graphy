"""Adapters package — Protocol contracts + mock/real implementations.

Public API:
    from audio_graphy.adapters import (
        VADAdapter, ASRAdapter, LLMAdapter, EmbedAdapter,
        VADSegment, ASRResult, LLMResponse, EmbeddingResult,
        AdapterBundle, EdgeConfidence,
        build_mock_bundle, build_hybrid_bundle,
    )
"""

from audio_graphy.adapters.bundle import (
    AdapterBundle,
    build_hybrid_bundle,
    build_mock_bundle,
)
from audio_graphy.adapters.protocols import (
    ASRAdapter,
    ASRResult,
    AudioEmbedAdapter,
    AudioEmbeddingResult,
    DiarizationResult,
    DiarizationSegment,
    EdgeConfidence,
    EmbedAdapter,
    EmbeddingResult,
    LLMAdapter,
    LLMResponse,
    VADAdapter,
    VADSegment,
    VoiceprintAdapter,
    VoiceprintResult,
)

__all__ = [
    "ASRAdapter",
    "ASRResult",
    "AdapterBundle",
    "AudioEmbedAdapter",
    "AudioEmbeddingResult",
    "DiarizationResult",
    "DiarizationSegment",
    "EdgeConfidence",
    "EmbedAdapter",
    "EmbeddingResult",
    "LLMAdapter",
    "LLMResponse",
    "VADAdapter",
    "VADSegment",
    "VoiceprintAdapter",
    "VoiceprintResult",
    "build_hybrid_bundle",
    "build_mock_bundle",
]
