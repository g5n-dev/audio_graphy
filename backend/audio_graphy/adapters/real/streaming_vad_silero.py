"""Streaming Silero VAD adapter — local ONNX session, 512-sample chunks, 4-state FSM.

M8 Phase 4 (WS-1 / T2). Streaming counterpart to ``adapters/real/vad_silero.py``
(which calls the HTTP server over a batch API). This adapter owns one ONNX
session per WebSocket connection and carries the Silero LSTM hidden state
chunk-to-chunk inside the instance.

Hard contract (L3 locked):
    - PCM format: 16 kHz mono int16 little-endian.
    - Chunk size: exactly 1024 bytes (512 samples × 2 bytes).
    - Hidden state: ``_state`` MUST be passed chunk-to-chunk; any gap
      triggers ``reset_state()`` (Q2 decision, ``StreamSession`` enforces).

FSM (PRD Appendix B):

    SILENCE --onset>=0.5--> PENDING_SPEECH --dur>=min_speech--> SPEECH
    SPEECH  --onset<0.35--> PENDING_SILENCE --dur>=min_silence--> SILENCE

Threshold defaults (L3): onset=0.5, offset=0.35, min_speech=0.25s, min_silence=0.10s.

The ONNX model is loaded lazily on first ``push_chunk()`` so instances can
be constructed cheaply at session creation time. ``onnxruntime`` is imported
lazily so that mock-only deployments do not require the package.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.exceptions import (
    StreamingVADChunkShapeError,
    StreamingVADModelLoadError,
    _redact,
)
from audio_graphy.adapters.protocols import (
    StreamingVADAdapter,
    VADEvent,
)

if TYPE_CHECKING:
    from audio_graphy.core.chunker import SegmentRecord

logger = logging.getLogger(__name__)

# L3 locked constants — do NOT change without dual-sign sign-off.
SILERO_SAMPLE_RATE: int = 16000
SILERO_CHUNK_SAMPLES: int = 512
SILERO_CHUNK_BYTES: int = SILERO_CHUNK_SAMPLES * 2  # int16 → 1024 bytes
SILERO_CHUNK_SEC: float = SILERO_CHUNK_SAMPLES / SILERO_SAMPLE_RATE  # 0.032s

# FSM state names (PRD Appendix B table).
_SILENCE = "SILENCE"
_PENDING_SPEECH = "PENDING_SPEECH"
_SPEECH = "SPEECH"
_PENDING_SILENCE = "PENDING_SILENCE"


@dataclass(slots=True)
class _SileroHiddenState:
    """Silero LSTM hidden state — h and c carry chunk-to-chunk.

    Initialized to zeros on construction and on ``reset_state()``. Replaced
    wholesale after each ONNX run (never mutated in place) so that a reset
    is a single attribute assignment.
    """

    h: Any  # onnxruntime requires numpy / torch tensor; opaque at type-level
    c: Any


@dataclass(slots=True)
class _VADFSM:
    """4-state finite-state-machine per PRD Appendix B.

    Attributes:
        state: Current FSM state name.
        onset_threshold: Above → counts as speech onset.
        offset_threshold: Below → counts as speech offset.
        min_speech_sec: PENDING_SPEECH → SPEECH promotion threshold.
        min_silence_sec: PENDING_SILENCE → SILENCE promotion threshold.
        pending_start_ts: Wall-clock second when PENDING_* state was entered.
        speech_start_ts: Wall-clock second when SPEECH was entered.
        chunk_sec: Per-chunk duration (constant, 0.032s).
    """

    state: str = _SILENCE
    onset_threshold: float = 0.5
    offset_threshold: float = 0.35
    min_speech_sec: float = 0.25
    min_silence_sec: float = 0.10
    pending_start_ts: float = 0.0
    speech_start_ts: float = 0.0
    chunk_sec: float = SILERO_CHUNK_SEC

    def step(self, onset: float, ts: float) -> tuple[str, str]:
        """Advance FSM by one chunk.

        Args:
            onset: Silero onset probability ∈ [0.0, 1.0].
            ts: Wall-clock second of this chunk.

        Returns:
            Tuple ``(new_state, transition)`` where transition ∈
            ``{"chunk", "segment_start", "segment_end"}``.
        """
        prev = self.state
        transition = "chunk"

        if prev == _SILENCE:
            if onset >= self.onset_threshold:
                self.state = _PENDING_SPEECH
                self.pending_start_ts = ts

        elif prev == _PENDING_SPEECH:
            if onset < self.onset_threshold:
                # Speech didn't last — abandon.
                self.state = _SILENCE
                self.pending_start_ts = 0.0
            elif ts - self.pending_start_ts >= self.min_speech_sec:
                # Promote to SPEECH; emit segment_start back-dated to pending_start.
                self.state = _SPEECH
                self.speech_start_ts = self.pending_start_ts
                self.pending_start_ts = 0.0
                transition = "segment_start"

        elif prev == _SPEECH:
            if onset < self.offset_threshold:
                self.state = _PENDING_SILENCE
                self.pending_start_ts = ts

        elif prev == _PENDING_SILENCE:
            if onset >= self.onset_threshold:
                # Speech resumed — abandon pending silence.
                self.state = _SPEECH
                self.pending_start_ts = 0.0
            elif ts - self.pending_start_ts >= self.min_silence_sec:
                # Silence confirmed — close the segment.
                self.state = _SILENCE
                transition = "segment_end"
                self.pending_start_ts = 0.0

        return self.state, transition


def _initial_hidden_state() -> _SileroHiddenState:
    """Construct a zero-initialised hidden state.

    Uses numpy arrays so we don't hard-depend on torch. The Silero ONNX
    model accepts numpy inputs directly (it was exported with dynamic axes).
    """
    import numpy as np

    # Silero VAD ONNX expects LSTM state shapes (2, 1, 64) for both h and c.
    return _SileroHiddenState(
        h=np.zeros((2, 1, 64), dtype=np.float32),
        c=np.zeros((2, 1, 64), dtype=np.float32),
    )


class StreamingSileroVADAdapter:
    """Real streaming VAD backed by ``silero_vad.onnx`` (local file).

    Lifecycle:
        - ONNX model loaded lazily on first ``push_chunk()``.
        - LSTM hidden state carried in ``self._state``.
        - Caller MUST invoke ``aclose()`` at WS close (frees ONNX session).

    Args:
        model_path: Path to ``silero_vad.onnx``.
        sample_rate: Always 16000 (Silero contract).
        chunk_samples: Always 512 (L3 locked).
        onset_threshold: Default 0.5 (L3).
        offset_threshold: Default 0.35 (L3).
        min_speech_sec: Default 0.25 (L3).
        min_silence_sec: Default 0.10 (L3).
    """

    def __init__(
        self,
        *,
        model_path: str = "/models/silero_vad.onnx",
        sample_rate: int = SILERO_SAMPLE_RATE,
        chunk_samples: int = SILERO_CHUNK_SAMPLES,
        onset_threshold: float = 0.5,
        offset_threshold: float = 0.35,
        min_speech_sec: float = 0.25,
        min_silence_sec: float = 0.10,
    ) -> None:
        self._model_path = model_path
        self._sample_rate = sample_rate
        self._chunk_samples = chunk_samples
        self._onset_threshold = onset_threshold
        self._offset_threshold = offset_threshold
        self._min_speech_sec = min_speech_sec
        self._min_silence_sec = min_silence_sec

        # Lazy state.
        self._sess: Any = None  # onnxruntime.InferenceSession
        self._state: _SileroHiddenState = _initial_hidden_state()
        self._fsm = _VADFSM(
            onset_threshold=onset_threshold,
            offset_threshold=offset_threshold,
            min_speech_sec=min_speech_sec,
            min_silence_sec=min_silence_sec,
        )

        # In-progress speech accumulator (set on segment_start, cleared on segment_end).
        self._speech_start_ts: float | None = None
        self._speech_pcm_buf: bytearray = bytearray()
        self._seq_offset: int = 0  # seq of the segment_start chunk

    # --------------------------------------------------------------
    # Protocol methods
    # --------------------------------------------------------------
    async def push_chunk(self, pcm: bytes, *, seq: int) -> VADEvent:
        """Feed one 512-sample PCM chunk and return the resulting VADEvent.

        Raises:
            StreamingVADChunkShapeError: ``len(pcm) != 1024``.
            StreamingVADModelLoadError: ONNX session could not be created.
        """
        if len(pcm) != SILERO_CHUNK_BYTES:
            raise StreamingVADChunkShapeError(
                f"PCM chunk must be exactly {SILERO_CHUNK_BYTES} bytes "
                f"(512 samples × 2), got {len(pcm)}",
                url=_redact(self._model_path),
            )

        self._ensure_session()
        ts = time.monotonic()

        onset_score = self._run_onnx(pcm)
        new_state, transition = self._fsm.step(onset_score, ts)

        # Accumulate speech PCM for the in-progress segment (used by finalize).
        if new_state in {_SPEECH, _PENDING_SILENCE} and self._speech_start_ts is not None:
            self._speech_pcm_buf.extend(pcm)

        segment = None
        if transition == "segment_start":
            self._speech_start_ts = self._fsm.speech_start_ts
            self._speech_pcm_buf = bytearray(pcm)
            self._seq_offset = seq
        elif transition == "segment_end":
            segment = self._close_segment(ts, seq)
            self._speech_start_ts = None
            self._speech_pcm_buf = bytearray()

        return VADEvent(
            seq=seq,
            timestamp_sec=ts,
            onset_score=onset_score,
            state=new_state,
            transition=transition,
            segment=segment,
        )

    def reset_state(self) -> None:
        """Reset LSTM hidden state + FSM.

        Called on seq gap > ``streaming_vad_reset_seq_gap`` (default 3) or
        explicit client ``reset`` control message. In-progress speech is
        abandoned (per PRD §17.6).
        """
        self._state = _initial_hidden_state()
        self._fsm = _VADFSM(
            onset_threshold=self._onset_threshold,
            offset_threshold=self._offset_threshold,
            min_speech_sec=self._min_speech_sec,
            min_silence_sec=self._min_silence_sec,
        )
        self._speech_start_ts = None
        self._speech_pcm_buf = bytearray()

    async def finalize(self) -> tuple[SegmentRecord, ...]:
        """Flush any in-progress speech segment.

        Returns:
            Tuple of 0 or 1 SegmentRecord (1 if FSM was in SPEECH or
            PENDING_SILENCE at finalize time).
        """
        if self._speech_start_ts is None:
            return ()
        ts = time.monotonic()
        seg = self._close_segment(ts, self._seq_offset)
        self._speech_start_ts = None
        self._speech_pcm_buf = bytearray()
        return (seg,)

    async def aclose(self) -> None:
        """Release ONNX session. Idempotent / 幂等关闭."""
        # onnxruntime.InferenceSession does not require explicit close, but
        # we drop the reference so GC can collect it. A subsequent push_chunk
        # after aclose() will re-create the session lazily.
        self._sess = None

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _ensure_session(self) -> None:
        if self._sess is not None:
            return
        model_path = Path(self._model_path)
        if not model_path.is_file():
            raise StreamingVADModelLoadError(
                f"silero_vad.onnx not found at {self._model_path}",
                url=_redact(self._model_path),
            )
        try:
            import onnxruntime as ort  # lazy import
        except ImportError as exc:
            raise StreamingVADModelLoadError(
                f"onnxruntime not installed: {exc}",
                url=_redact(self._model_path),
            ) from exc

        try:
            # thread_per_session avoids GPU contention on CPU-only deployments.
            self._sess = ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            logger.debug("Silero ONNX session loaded path=%s", _redact(self._model_path))
        except Exception as exc:
            raise StreamingVADModelLoadError(
                f"Failed to create ONNX session: {exc}",
                url=_redact(self._model_path),
            ) from exc

    def _run_onnx(self, pcm: bytes) -> float:
        """Run one ONNX inference step, update hidden state, return onset.

        Silero VAD ONNX input/output names follow the community-exported
        convention (``input`` / ``h`` / ``c`` → ``output`` / ``hn`` / ``cn``).
        Different community exports use slightly different names; the code
        below tolerates the two most common variants.
        """
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # Silero expects shape (1, N) for the input samples.
        samples = samples.reshape(1, -1)

        input_name = self._find_input_name(("input", "audio", "waveform"))
        h_name = self._find_input_name(("h", "hn", "lstm_h"))
        c_name = self._find_input_name(("c", "cn", "lstm_c"))

        try:
            feeds = {
                input_name: samples,
                h_name: self._state.h,
                c_name: self._state.c,
            }
            outputs = self._sess.run(None, feeds)
        except Exception as exc:  # ONNX runtime errors are RuntimeError subclasses
            logger.warning("Silero ONNX run failed, resetting state: %s", exc)
            self.reset_state()
            return 0.0

        # The output order is (prob, h_new, c_new) in community exports.
        onset = float(np.squeeze(outputs[0]))
        # Update hidden state with returned tensors.
        if len(outputs) >= 3:
            self._state = _SileroHiddenState(h=outputs[1], c=outputs[2])
        return max(0.0, min(1.0, onset))

    def _find_input_name(self, candidates: Sequence[str]) -> str:
        for name in candidates:
            for meta in self._sess.get_inputs():
                if meta.name == name:
                    return str(name)
        # Fallback: first input.
        return str(self._sess.get_inputs()[0].name)

    def _find_output_name(self, candidates: Sequence[str]) -> str:
        for name in candidates:
            for meta in self._sess.get_outputs():
                if meta.name == name:
                    return str(name)
        return str(self._sess.get_outputs()[0].name)

    def _close_segment(self, ts: float, end_seq: int) -> SegmentRecord:
        """Build a SegmentRecord for the just-closed speech segment."""
        # Lazy import to avoid a hard cycle (core.chunker imports from adapters).
        from audio_graphy.core.chunker import SegmentRecord

        start_ts = self._speech_start_ts if self._speech_start_ts is not None else ts
        # PCM buffer may be empty if segment was opened but no further chunks arrived.
        transcript = ""  # ASR fills this in later; VAD leaves it empty.
        return SegmentRecord(
            idx=self._seq_offset,
            start_sec=start_ts,
            end_sec=ts,
            transcript=transcript,
            speaker=None,
            vad_conf=1.0,
        )


# Protocol satisfaction check (fails at import if drift).
_STREAMING_VAD_PROTOCOL_CHECK: StreamingVADAdapter = StreamingSileroVADAdapter()
