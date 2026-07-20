"""Adapter Protocols — the contract every ASR/LLM/Embed/VAD implementation must satisfy.

Mock implementations (default, ADAPTER_MODE=mock) live in `audio_graphy/adapters/mock_*.py`.
Real implementations (ADAPTER_MODE=real) are deferred to a later sprint.

Design:
- Protocols use `async` everywhere — real services are network-bound.
- Mocks simulate latency (deterministic via hash) and 1% flakiness by default.
- All return types are frozen dataclasses for type safety + immutability.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# ============================================================
# Result dataclasses
# ============================================================


@dataclass(frozen=True, slots=True)
class VADSegment:
    """A voice-active segment returned by the VAD adapter."""

    start_sec: float
    end_sec: float
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class ASRResult:
    """A single ASR transcript with timing."""

    text: str
    language: str = "zh"
    confidence: float = 0.95
    # Word-level timestamps (optional — only some ASR services return them)
    words: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A response from an LLM call."""

    text: str
    model: str
    prompt_hash: str  # MD5 of (model, messages) — used for cache key
    cached: bool = False  # True if returned from LLM cache (no API call made)
    usage: dict[str, int] = field(default_factory=dict)  # {prompt_tokens, completion_tokens}


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """A vector + its source identifier."""

    vector: tuple[float, ...]
    dim: int
    model: str


# ============================================================
# Protocols
# ============================================================


@runtime_checkable
class VADAdapter(Protocol):
    """Voice Activity Detection — splits audio into voice-active segments."""

    async def segment(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = 0.5,
        max_segment_sec: float = 30.0,
    ) -> Sequence[VADSegment]: ...


@runtime_checkable
class ASRAdapter(Protocol):
    """Automatic Speech Recognition — transcribes audio segments."""

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "zh",
    ) -> ASRResult: ...


@runtime_checkable
class LLMAdapter(Protocol):
    """LLM chat completion — used for entity extraction / Q&A / tagging."""

    model: str

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class EmbedAdapter(Protocol):
    """Embedding — encodes text into a vector for similarity search."""

    model: str
    dim: int

    async def embed_texts(
        self,
        texts: Sequence[str],
    ) -> Sequence[EmbeddingResult]: ...


# ============================================================
# Confidence tags (borrowed from Graphify — see DESIGN.md §3.1)
# ============================================================

EdgeConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
"""Confidence tag attached to every edge in the knowledge graph.

- EXTRACTED: relation is explicitly stated in source transcript (confidence 1.0)
- INFERRED: relation derived via cross-segment merge / clustering (0.0 < score < 1.0)
- AMBIGUOUS: uncertain pairing, flagged for human review
"""
