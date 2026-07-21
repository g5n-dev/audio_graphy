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
    EdgeConfidence,
    EmbedAdapter,
    EmbeddingResult,
    LLMAdapter,
    LLMResponse,
    VADAdapter,
    VADSegment,
)

__all__ = [
    "ASRAdapter",
    "ASRResult",
    "AdapterBundle",
    "EdgeConfidence",
    "EmbedAdapter",
    "EmbeddingResult",
    "LLMAdapter",
    "LLMResponse",
    "VADAdapter",
    "VADSegment",
    "build_hybrid_bundle",
    "build_mock_bundle",
]
