"""Adapters package — Protocol contracts + mock/real implementations.

Public API:
    from audio_graphy.adapters import (
        VADAdapter, ASRAdapter, LLMAdapter, EmbedAdapter,
        VADSegment, ASRResult, LLMResponse, EmbeddingResult,
        AdapterBundle, EdgeConfidence,
    )
"""

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
]
