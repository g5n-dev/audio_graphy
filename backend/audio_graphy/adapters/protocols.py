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
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from audio_graphy.core.chunker import SegmentRecord

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
# M8 Phase 4 — Streaming adapter dataclasses + Protocols
# ============================================================


@dataclass(frozen=True, slots=True)
class VADEvent:
    """One event yielded by ``StreamingVADAdapter.push_chunk()``.

    Attributes:
        seq: Chunk sequence number (echoed from client).
        timestamp_sec: Wall-clock timestamp of the chunk arrival.
        onset_score: Silero raw onset probability ∈ [0.0, 1.0].
        state: Current FSM state — ``"SILENCE"`` / ``"PENDING_SPEECH"``
            / ``"SPEECH"`` / ``"PENDING_SILENCE"``.
        transition: Event type — ``"chunk"`` (no boundary) /
            ``"segment_start"`` / ``"segment_end"``.
        segment: When ``transition == "segment_end"``, carries the
            just-closed SegmentRecord. ``None`` otherwise.
        reset: True if the FSM was reset on this chunk (seq gap or explicit).
    """

    seq: int
    timestamp_sec: float
    onset_score: float
    state: str
    transition: str
    segment: SegmentRecord | None = None
    reset: bool = False


@dataclass(frozen=True, slots=True)
class ASRDeltaResult:
    """One delta yielded by ``StreamingASRAdapter.push_pcm()``.

    funASR returns two flavours of delta:
        - realtime (``mode="2pass-online"``): partial transcript, may be revised.
        - confirmed (``mode="2pass-offline"``, ``is_final=True``): sentence-final.

    Attributes:
        seq: Last PCM seq consumed by this delta.
        mode: ``"realtime"`` / ``"confirmed"``.
        text: Transcript text (incremental for realtime, full for confirmed).
        is_final: True when this finishes a confirmed sentence.
        sentence_id: funASR sentence index (for grouping realtime→confirmed).
        confidence: ASR confidence if reported by funASR (else 0.95).
    """

    seq: int
    mode: str  # "realtime" | "confirmed"
    text: str
    is_final: bool
    sentence_id: int
    confidence: float = 0.95


@dataclass(frozen=True, slots=True)
class StreamSessionId:
    """Opaque per-session identifier (UUID v4 generated client-side).

    Used for SessionState persistence/reconnect and audit trail. NOT the
    same as the DB row id (``streaming_sessions.id`` BIGSERIAL).
    """

    value: str


@runtime_checkable
class StreamingVADAdapter(Protocol):
    """Streaming VAD — consumes PCM chunks, yields VAD events.

    M8 default impl: Silero VAD streaming (``silero_vad.onnx``), 512
    samples / chunk (32 ms @ 16 kHz), 4-state FSM (PRD Appendix B).
    LSTM hidden state MUST be carried chunk-to-chunk inside the adapter
    instance.

    Lifecycle:
        - Adapter is bound to one SessionState (per WS connection).
        - ``reset_state()`` may be called between chunks if seq gap detected.
        - ``finalize()`` flushes any in-flight speech segment.

    Raises:
        StreamingVADChunkShapeError: PCM chunk not multiple of 512 samples.
        StreamingVADModelLoadError: ``silero_vad.onnx`` missing / corrupt.
    """

    async def push_chunk(
        self,
        pcm: bytes,
        *,
        seq: int,
    ) -> VADEvent:
        """Feed one 512-sample PCM chunk, return the resulting VAD event.

        Args:
            pcm: 16-bit little-endian PCM, 16 kHz mono, length MUST be
                exactly 1024 bytes (512 samples × 2 bytes).
            seq: Client-supplied monotonic sequence number.
        """
        ...

    def reset_state(self) -> None:
        """Reset LSTM hidden state + FSM.

        Called on seq gap > ``streaming_vad_reset_seq_gap`` (default 3)
        or explicit client ``reset`` control message.
        """
        ...

    async def finalize(self) -> tuple[SegmentRecord, ...]:
        """Flush any in-progress speech segment at connection close.

        Returns:
            Tuple of SegmentRecord (may be empty if no pending speech).
        """
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class StreamingASRAdapter(Protocol):
    """Streaming ASR — consumes PCM, yields realtime/confirmed deltas.

    M8 default impl: funASR ``paraformer-zh-streaming`` over
    WebSocket:10095 (PRD Appendix A). Adapter owns ONE WebSocket per session.

    Behaviour:
        - ``connect()`` opens funASR WS, sends init JSON
          (``mode=2pass``, ``chunk_size=[5,10,5]``, hotwords from tenant entity_aliases).
        - ``push_pcm()`` sends binary, awaits next JSON delta, maps to
          ASRDeltaResult (realtime or confirmed).
        - ``finalize()`` sends ``{"is_speaking": false}``, drains pending
          deltas until final confirmed arrives.
        - Tenant isolation: per-tenant pool (Q1 decision, pool_size=8).
    """

    async def connect(
        self,
        *,
        session_id: str,
        tenant_id: str,
        hotwords: Sequence[str] = (),
    ) -> None:
        """Open funASR WebSocket and send init handshake."""
        ...

    async def push_pcm(
        self,
        pcm: bytes,
        *,
        seq: int,
    ) -> ASRDeltaResult:
        """Send binary PCM chunk, await next delta from funASR."""
        ...

    async def finalize(self) -> tuple[ASRDeltaResult, ...]:
        """Send ``is_speaking=false``, drain remaining deltas (typically 0-2)."""
        ...

    async def aclose(self) -> None: ...


# ============================================================
# Confidence tags (borrowed from Graphify — see DESIGN.md §3.1)
# ============================================================

EdgeConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
"""Confidence tag attached to every edge in the knowledge graph.

- EXTRACTED: relation is explicitly stated in source transcript (confidence 1.0)
- INFERRED: relation derived via cross-segment merge / clustering (0.0 < score < 1.0)
- AMBIGUOUS: uncertain pairing, flagged for human review
"""
