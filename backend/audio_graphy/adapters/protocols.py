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
# M7 Phase 2 — Audio embedding + voiceprint result dataclasses
# ============================================================


@dataclass(frozen=True, slots=True)
class AudioEmbeddingResult:
    """CLAP audio embedding for one segment.

    Attributes:
        vector: 512-dim L2-normalized CLAP embedding (float32).
        dim: Always 512 for laion_clap HTSAT-base (L1 locked).
        model: Model identifier (e.g. ``"clap-htsat-base-2022"``).
        segment_id: Optional segment index this embedding corresponds to.
        duration_sec: Audio duration in seconds (for metrics).
    """

    vector: tuple[float, ...]
    dim: int
    model: str
    segment_id: int | None = None
    duration_sec: float = 0.0


@dataclass(frozen=True, slots=True)
class DiarizationSegment:
    """One segment from CAM++ diarization, tagged with a speaker label.

    Attributes:
        start_sec / end_sec: Time window (file-relative).
        speaker_id: Stable per-file speaker label (e.g. ``"spk_0"``).
            NOT cross-recording linked yet — ``SpeakerLinker`` does that.
        confidence: Diarization confidence in [0.0, 1.0].
    """

    start_sec: float
    end_sec: float
    speaker_id: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class DiarizationResult:
    """Full diarization timeline output by VoiceprintAdapter.diarize.

    Attributes:
        segments: Tuple of DiarizationSegment (ordered by start_sec).
        num_speakers: Count of distinct speaker IDs in ``segments``.
        model: Model identifier reported by the service.
        duration_sec: Audio duration in seconds.
    """

    segments: tuple[DiarizationSegment, ...]
    num_speakers: int
    model: str
    duration_sec: float = 0.0


@dataclass(frozen=True, slots=True)
class VoiceprintResult:
    """192-d L2-normalized CAM++ speaker voiceprint.

    Attributes:
        vector: 192-dim CAM++ embedding (L2-normalized → cosine == dot product).
        dim: Always 192 for iic/speech_campplus_sv_zh-cn_16k-common (L2 locked).
        model: Model identifier.
        speaker_id: Same speaker_id as the source diarization segment (if any).
        duration_sec: Audio duration used for extraction (quality signal).
    """

    vector: tuple[float, ...]
    dim: int
    model: str
    speaker_id: str = ""
    duration_sec: float = 0.0


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
        segments: list[VADSegment] | None = None,
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
# M7 Phase 2 — Audio embedding + voiceprint Protocols
# ============================================================


@runtime_checkable
class AudioEmbedAdapter(Protocol):
    """Audio embedding — encodes audio segments into vectors for similarity search.

    M7 default impl: CLAP HTSAT-base (laion_clap), 48 kHz mono, 512-d output.
    The backing service is responsible for resampling to 48 kHz before model
    inference; clients submit the original audio file.

    Implementations MUST satisfy the contract:

    - ``embed_audio([path1, path2, ...])`` returns one ``AudioEmbeddingResult``
      per input path (positional correspondence).
    - Output vectors are L2-normalized so cosine similarity == dot product.
    """

    model: str
    dim: int  # always 512 (L1 locked)

    async def embed_audio(
        self,
        audio_paths: Sequence[str],
        *,
        segment_ids: Sequence[int | None] | None = None,
    ) -> Sequence[AudioEmbeddingResult]: ...


@runtime_checkable
class VoiceprintAdapter(Protocol):
    """Speaker voiceprint extraction + diarization (CAM++).

    M7 default impl: ``iic/speech_campplus_sv_zh-cn_16k-common`` (192-d,
    L2-normalized). The backing service is responsible for resampling to
    16 kHz mono; clients submit the original audio file.

    ``diarize`` and ``extract_voiceprint`` are two independent endpoints —
    callers may invoke either or both. ``diarize`` produces a timeline of
    speaker-tagged segments for a full file; ``extract_voiceprint``
    produces a single 192-d voiceprint for an audio file (optionally
    cropped to ``[start_sec, end_sec]`` server-side).
    """

    model: str
    dim: int  # always 192 (L2 locked)

    async def diarize(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = 0.5,
        max_speakers: int = 10,
    ) -> DiarizationResult: ...

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> VoiceprintResult: ...


# ============================================================
# Confidence tags (borrowed from Graphify — see DESIGN.md §3.1)
# ============================================================

EdgeConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
"""Confidence tag attached to every edge in the knowledge graph.

- EXTRACTED: relation is explicitly stated in source transcript (confidence 1.0)
- INFERRED: relation derived via cross-segment merge / clustering (0.0 < score < 1.0)
- AMBIGUOUS: uncertain pairing, flagged for human review
"""
