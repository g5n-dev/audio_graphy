"""Mock streaming VAD adapter — deterministic VADEvent from sha512(pcm).

M8 Phase 4 (WS-1 / T2). CI-friendly counterpart to
``adapters/real/streaming_vad_silero.py``. Produces the same VADEvent shape
without the onnxruntime dependency.

Determinism contract:
    - Same PCM bytes → same onset_score, same state transition.
    - Pre-canned pattern simulates ~6.4s speech segments:
        * Every 50th chunk (1.6s @ 32ms/chunk): ``segment_start``.
        * Every 200th chunk (6.4s): ``segment_end``.
    - ``reset_state()`` clears the pattern counters so post-reset sequences
      are reproducible regardless of pre-reset state.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING

from audio_graphy.adapters.protocols import (
    StreamingVADAdapter,
    VADEvent,
)

if TYPE_CHECKING:
    from audio_graphy.core.chunker import SegmentRecord

logger = logging.getLogger(__name__)

_SILENCE = "SILENCE"
_PENDING_SPEECH = "PENDING_SPEECH"
_SPEECH = "SPEECH"
_PENDING_SILENCE = "PENDING_SILENCE"

CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 1024
CHUNK_SEC = CHUNK_SAMPLES / 16000.0  # 0.032


class MockStreamingVADAdapter:
    """Deterministic mock streaming VAD — no ONNX, no GPU, CI-friendly.

    The FSM mimics the real Silero 4-state machine but transitions are driven
    by chunk count rather than onset probabilities. This makes the full
    VAD → ASR → confirmed segment pipeline testable in mock mode with
    predictable event counts (see PRD §5.1 / architecture §5.1).

    Args:
        chunk_samples: Always 512 (L3). Kept as a parameter so mis-config
            is caught early.
        latency_ms: Simulated per-chunk latency (default 5ms).
        flaky: When True, every 100th chunk raises a synthetic error to
            exercise error paths.
    """

    def __init__(
        self,
        *,
        chunk_samples: int = CHUNK_SAMPLES,
        latency_ms: float = 5.0,
        flaky: bool = False,
    ) -> None:
        if chunk_samples != CHUNK_SAMPLES:
            raise ValueError(
                f"MockStreamingVADAdapter chunk_samples must be {CHUNK_SAMPLES}, "
                f"got {chunk_samples}"
            )
        self._latency_sec = latency_ms / 1000.0
        self._flaky = flaky
        self._chunk_count = 0
        self._state = _SILENCE
        self._speech_start_seq: int | None = None
        self._speech_start_ts: float | None = None
        # Buffer of (seq, ts, pcm) tuples for the in-progress segment.
        self._pending_chunks: list[tuple[int, float, bytes]] = []

    async def push_chunk(self, pcm: bytes, *, seq: int) -> VADEvent:
        """Process one PCM chunk, yield one VADEvent.

        Simulates fixed latency, deterministic state transitions.
        """
        await asyncio.sleep(self._latency_sec)

        if self._flaky and self._chunk_count > 0 and self._chunk_count % 100 == 0:
            raise RuntimeError(
                f"MockStreamingVAD flaky mode triggered at chunk={self._chunk_count}"
            )

        if len(pcm) != CHUNK_BYTES:
            from audio_graphy.adapters.exceptions import StreamingVADChunkShapeError

            raise StreamingVADChunkShapeError(
                f"PCM chunk must be exactly {CHUNK_BYTES} bytes, got {len(pcm)}",
            )

        self._chunk_count += 1
        ts = time.monotonic()

        # Deterministic onset: hash the PCM to produce a stable score.
        digest = hashlib.sha512(pcm).hexdigest()
        onset_bucket = int(digest[:4], 16) / 0xFFFF  # 0.0..1.0

        transition, segment = self._step_mock_fsm(seq, ts, onset_bucket)
        return VADEvent(
            seq=seq,
            timestamp_sec=ts,
            onset_score=round(onset_bucket, 4),
            state=self._state,
            transition=transition,
            segment=segment,
        )

    def reset_state(self) -> None:
        """Reset pattern counters + FSM."""
        self._chunk_count = 0
        self._state = _SILENCE
        self._speech_start_seq = None
        self._speech_start_ts = None
        self._pending_chunks = []

    async def finalize(self) -> tuple[SegmentRecord, ...]:
        """Flush any in-progress speech segment."""
        if self._speech_start_ts is None or not self._pending_chunks:
            return ()
        first_seq, first_ts, _ = self._pending_chunks[0]
        last_seq, last_ts, _ = self._pending_chunks[-1]
        seg = self._build_segment(first_seq, first_ts, last_ts)
        # Clear state without resetting the chunk counter (keeps the
        # post-finalize sequence contiguous with pre-finalize for audit).
        self._speech_start_seq = None
        self._speech_start_ts = None
        self._pending_chunks = []
        self._state = _SILENCE
        logger.debug("MockStreamingVAD finalize segment seq=%d..%d", first_seq, last_seq)
        return (seg,)

    async def aclose(self) -> None:
        """No-op (no resources to release)."""
        return

    # ------------------------------------------------------------------
    # Mock FSM
    # ------------------------------------------------------------------
    def _step_mock_fsm(
        self,
        seq: int,
        ts: float,
        onset: float,
    ) -> tuple[str, SegmentRecord | None]:
        """Step the mock 4-state FSM.

        Pattern:
            - chunk % 50 == 0 (and state SILENCE) → start speech.
            - chunk % 200 == 0 (and state SPEECH) → end speech.
            - otherwise advance; onset is reported but does not drive transitions.
        """
        transition = "chunk"
        segment: SegmentRecord | None = None

        # Drive transition on chunk index modulo.
        if self._state == _SILENCE and self._chunk_count % 50 == 0:
            self._state = _PENDING_SPEECH
            # Mock fast-promote: jump straight to SPEECH in one step so tests see segment_start.
            self._state = _SPEECH
            self._speech_start_seq = seq
            self._speech_start_ts = ts
            self._pending_chunks = [(seq, ts, b"")]
            transition = "segment_start"

        elif self._state == _SPEECH:
            # Accumulate.
            self._pending_chunks.append((seq, ts, b""))
            if self._chunk_count % 200 == 0:
                # Close the segment.
                first_seq, first_ts, _ = self._pending_chunks[0]
                segment = self._build_segment(first_seq, first_ts, ts)
                transition = "segment_end"
                self._speech_start_seq = None
                self._speech_start_ts = None
                self._pending_chunks = []
                self._state = _SILENCE

        return transition, segment

    @staticmethod
    def _build_segment(start_seq: int, start_ts: float, end_ts: float) -> SegmentRecord:
        from audio_graphy.core.chunker import SegmentRecord

        return SegmentRecord(
            idx=start_seq,
            start_sec=start_ts,
            end_sec=end_ts,
            transcript="",
            speaker=None,
            vad_conf=1.0,
        )


# Protocol satisfaction check (fails at import if drift).
_MOCK_STREAMING_VAD_PROTOCOL_CHECK: StreamingVADAdapter = MockStreamingVADAdapter()
