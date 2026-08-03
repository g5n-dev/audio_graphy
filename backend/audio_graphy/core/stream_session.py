"""StreamSession — per-WebSocket-connection session state (M8 P0-3).

Holds streaming VAD + ASR adapters, accumulates PCM + realtime + confirmed
buffers, tracks seq-gap detection, and enforces PIPL memory caps.

Lifecycle (architecture §7.1.2):

    CREATED ── init frame parsed ──▶ ACTIVE
    ACTIVE ── finalize / disconnect / timeout / backpressure ──▶ DRAINING
    DRAINING ── VAD/ASR finalize done ──▶ CLOSED

Memory caps (PRD §5.3 PIPL):

    - ``pending_speech_pcm``: 60s × 16kB/s = 960KB; overflow drops earliest.
    - ``pending_realtime``: 5 entries (frontend display window).
    - ``confirmed_segments``: 30 entries trigger forced flush to chunker.
    - Total per-session budget ≤ 5MB (excludes shared ONNX model).

Seq-gap detection (Q2):

    On each ``on_pcm_chunk``: if ``seq - last_seq > streaming_vad_reset_seq_gap``
    (default 3), call ``vad_adapter.reset_state()`` and emit a ``vad_reset``
    server event. In-progress speech is abandoned.

This module is intentionally synchronous-friendly on the FSM side but
exposes ``on_pcm_chunk`` as an async generator (yields server events to send
down the WebSocket).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from audio_graphy.adapters.protocols import (
    ASRDeltaResult,
    StreamingASRAdapter,
    StreamingVADAdapter,
    StreamSessionId,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SessionStatus(StrEnum):
    """Session lifecycle status (architecture §7.1.2)."""

    CREATED = "created"
    ACTIVE = "active"
    DRAINING = "draining"
    COMMITTING = "committing"
    CLOSED = "closed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


# PIPL memory caps (PRD §5.3).
PCM_BYTES_PER_SEC_16K_MONO_INT16 = 16000 * 2  # 32,000 bytes/sec
DEFAULT_PCM_BUFFER_MAX_SEC = 60.0  # 960KB cap
DEFAULT_REALTIME_WINDOW = 5  # 5 most-recent realtime deltas
DEFAULT_CONFIRMED_FLUSH_THRESHOLD = 30  # force chunker flush


@dataclass
class StreamSession:
    """Per-WebSocket-connection session state.

    Not thread-safe — one asyncio task per session. Created on init frame,
    destroyed on session close.

    Attributes:
        session_id: Opaque client-supplied UUID.
        tenant_id: Tenant scope (from JWT).
        recording_id: Associated recording row id.
        user_id: User id from JWT (None if anon).
        consent_token_hash: sha256(consent_token) — required by PRD §5.3 R10.
        vad_adapter: StreamingVADAdapter (mock or real).
        asr_adapter: StreamingASRAdapter (mock or real).
        seq_gap_threshold: Max seq jump before VAD reset (default 3 per Q2).
        pcm_buffer_max_sec: PCM buffer cap in seconds (default 60).
        realtime_window: Max pending_realtime entries (default 5).
        confirmed_flush_threshold: Force chunker flush at N confirmed (default 30).
    """

    session_id: StreamSessionId
    tenant_id: str
    recording_id: int
    user_id: int | None
    consent_token_hash: str

    vad_adapter: StreamingVADAdapter
    asr_adapter: StreamingASRAdapter

    seq_gap_threshold: int = 3
    pcm_buffer_max_sec: float = DEFAULT_PCM_BUFFER_MAX_SEC
    realtime_window: int = DEFAULT_REALTIME_WINDOW
    confirmed_flush_threshold: int = DEFAULT_CONFIRMED_FLUSH_THRESHOLD
    close_asr_on_finalize: bool = True
    epoch: int = 1
    generation: int = 0
    pipeline_run_id: int | None = None
    lease_token: str | None = None
    lease_ttl_seconds: float = 120.0
    durable_segment_high_watermark: int = 0
    persistence_id: int | None = None

    # Runtime state — mutated during ACTIVE.
    status: SessionStatus = SessionStatus.CREATED
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_chunk_at: datetime | None = None
    last_seq: int = -1
    bytes_in: int = 0
    seg_confirmed_count: int = 0
    seg_realtime_count: int = 0
    error_count: int = 0
    end_reason: str | None = None

    # Buffers.
    pending_speech_pcm: bytearray = field(default_factory=bytearray)
    pending_realtime: list[ASRDeltaResult] = field(default_factory=list)
    confirmed_segments: list[Any] = field(default_factory=list)  # SegmentRecord list
    pending_confirmed: list[ASRDeltaResult] = field(default_factory=list)
    confirmed_segment_cursor: int = 0
    accepted_sequences: set[int] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def mark_active(self) -> None:
        """Transition CREATED → ACTIVE."""
        if self.status != SessionStatus.CREATED:
            return
        self.status = SessionStatus.ACTIVE

    async def on_pcm_chunk(self, pcm: bytes, *, seq: int) -> AsyncIterator[dict[str, Any]]:
        """Process one PCM chunk, yield 0..N server-to-client events.

        Server events yielded:
            - ``{"type": "vad_reset", ...}`` if seq-gap triggered a reset.
            - ``{"type": "realtime_text", ...}`` for each realtime delta.
            - ``{"type": "segment_confirmed", ...}`` for each confirmed delta.

        The caller (WebSocket endpoint) is responsible for serialising the
        events to JSON and pushing them down the wire.

        Args:
            pcm: 1024-byte PCM chunk (16kHz mono int16).
            seq: Client-supplied monotonic sequence number.

        Yields:
            Server-event dicts.
        """
        if self.status not in (SessionStatus.ACTIVE, SessionStatus.CREATED):
            logger.warning(
                "StreamSession.on_pcm_chunk called in status=%s (session=%s)",
                self.status.value,
                self.session_id.value,
            )
            return

        # Network frames are at-least-once.  Never feed a duplicate or a
        # regressing frame into VAD/ASR: both adapters are stateful and doing
        # so would duplicate durable speech or corrupt their internal clocks.
        if seq in self.accepted_sequences:
            yield {
                "type": "frame_ack",
                "session_id": self.session_id.value,
                "seq": seq,
                "duplicate": True,
            }
            return
        if self.last_seq >= 0 and seq < self.last_seq:
            yield {
                "type": "error",
                "session_id": self.session_id.value,
                "code": "OUT_OF_ORDER_SEQ",
                "message": "sequence number is below the accepted watermark",
                "recoverable": True,
                "seq": seq,
                "accepted_seq_high_watermark": self.last_seq,
            }
            return

        self.mark_active()
        self.bytes_in += len(pcm)
        self.last_chunk_at = datetime.now(UTC)

        # --- Step 1: seq-gap detection (Q2) ---
        reset_triggered = False
        if self.last_seq >= 0 and (seq - self.last_seq) > self.seq_gap_threshold:
            self.vad_adapter.reset_state()
            reset_triggered = True
            logger.info(
                "StreamSession seq-gap reset: last=%d seq=%d gap=%d > %d (session=%s)",
                self.last_seq,
                seq,
                seq - self.last_seq,
                self.seq_gap_threshold,
                self.session_id.value,
            )
            yield {
                "type": "vad_reset",
                "session_id": self.session_id.value,
                "seq": seq,
                "reason": "seq_gap",
                "gap": seq - self.last_seq,
                "timestamp_ms": int(time.time() * 1000),
            }
            # In-progress speech is abandoned on reset.
            self.pending_speech_pcm = bytearray()

        self.last_seq = seq
        self.accepted_sequences.add(seq)
        if len(self.accepted_sequences) > 4_096:
            oldest_retained = self.last_seq - 4_095
            self.accepted_sequences = {
                accepted for accepted in self.accepted_sequences if accepted >= oldest_retained
            }

        # --- Step 2: VAD ---
        vad_event = await self.vad_adapter.push_chunk(pcm, seq=seq)
        if vad_event.transition == "segment_end" and vad_event.segment is not None:
            # Buffer the closed segment for the chunker (full PCM not retained —
            # DeltaGraphUpdater uses transcript, not audio, for entity extraction).
            self.confirmed_segments.append(vad_event.segment)
            self._enforce_confirmed_cap()
            for event in self._drain_confirmed_pairs():
                yield event

        # Accumulate PCM for in-progress speech (only if not just reset).
        if not reset_triggered and vad_event.state in ("SPEECH", "PENDING_SILENCE"):
            self.pending_speech_pcm.extend(pcm)
            self._enforce_pcm_cap()

        # --- Step 3: ASR ---
        try:
            delta = await self.asr_adapter.push_pcm(pcm, seq=seq)
        except Exception as exc:
            self.error_count += 1
            logger.warning(
                "StreamSession ASR push failed seq=%d session=%s: %s",
                seq,
                self.session_id.value,
                exc,
            )
            yield {
                "type": "error",
                "session_id": self.session_id.value,
                "code": "ASR_PUSH_FAILED",
                "message": str(exc),
                "recoverable": True,
                "seq": seq,
            }
            yield {
                "type": "frame_ack",
                "session_id": self.session_id.value,
                "seq": seq,
                "duplicate": False,
            }
            return

        # --- Step 4: yield server events based on delta ---
        if delta.mode == "realtime" and delta.text:
            self.seg_realtime_count += 1
            self._push_realtime(delta)
            yield {
                "type": "realtime_text",
                "session_id": self.session_id.value,
                "seq": seq,
                "text": delta.text,
                "is_final": False,
                "sentence_id": delta.sentence_id,
                "timestamp_ms": int(time.time() * 1000),
            }
        elif delta.mode == "confirmed" and delta.is_final:
            # ASR and VAD close independently.  Keep the confirmed transcript
            # until a real VAD segment exists; emitting before that point used
            # to acknowledge text that was never attached to durable geometry.
            self.pending_confirmed.append(delta)
            for event in self._drain_confirmed_pairs():
                yield event
        yield {
            "type": "frame_ack",
            "session_id": self.session_id.value,
            "seq": seq,
            "duplicate": False,
        }

    async def on_control_reset(self) -> AsyncIterator[dict[str, Any]]:
        """Handle client-initiated VAD reset.

        Yields:
            One ``vad_reset`` event.
        """
        self.vad_adapter.reset_state()
        self.pending_speech_pcm = bytearray()
        yield {
            "type": "vad_reset",
            "session_id": self.session_id.value,
            "reason": "client_request",
            "timestamp_ms": int(time.time() * 1000),
        }

    async def on_finalize(self) -> AsyncIterator[dict[str, Any]]:
        """Drain pending state, emit final events, transition to CLOSED.

        Yields:
            Server events from the trailing ASR deltas (typically 0-2
            ``segment_confirmed`` events).
        """
        if self.status == SessionStatus.CLOSED:
            return
        self.status = SessionStatus.DRAINING

        # Flush in-progress speech via VAD finalize.
        try:
            trailing_segments = await self.vad_adapter.finalize()
            for seg in trailing_segments:
                self.confirmed_segments.append(seg)
                self._enforce_confirmed_cap()
        except Exception as exc:
            self.error_count += 1
            logger.warning(
                "StreamSession VAD finalize failed session=%s: %s",
                self.session_id.value,
                exc,
            )

        # Drain trailing ASR deltas.
        try:
            trailing_deltas = await self.asr_adapter.finalize()
            for delta in trailing_deltas:
                if delta.mode == "confirmed" and delta.is_final:
                    self.pending_confirmed.append(delta)
        except Exception as exc:
            self.error_count += 1
            logger.warning(
                "StreamSession ASR finalize failed session=%s: %s",
                self.session_id.value,
                exc,
            )

        for event in self._drain_confirmed_pairs():
            yield event

        # Adapter cleanup is deliberately independent: one faulty adapter must
        # never prevent the other from releasing its process/socket resources.
        adapters_to_close: list[tuple[str, Any]] = [("VAD", self.vad_adapter)]
        if self.close_asr_on_finalize:
            adapters_to_close.append(("ASR", self.asr_adapter))
        for name, adapter in adapters_to_close:
            try:
                await adapter.aclose()
            except Exception as exc:
                self.error_count += 1
                logger.warning(
                    "StreamSession %s close failed session=%s: %s",
                    name,
                    self.session_id.value,
                    exc,
                )

        self.status = SessionStatus.CLOSED
        self.end_reason = self.end_reason or "normal"

    def mark_end(self, *, reason: str) -> None:
        """Force transition to CLOSED with a specific end reason.

        Used by the WebSocket endpoint on disconnect / backpressure / timeout.
        """
        if self.status == SessionStatus.CLOSED:
            return
        self.status = SessionStatus.CLOSED
        self.end_reason = reason

    def begin_drain(self, *, reason: str) -> None:
        """Record an exit reason while still allowing deterministic finalize.

        WebSocket disconnects and timeouts must pass through ``on_finalize``;
        directly marking them closed would skip trailing VAD/ASR data and
        adapter cleanup.
        """
        if self.status == SessionStatus.CLOSED:
            return
        self.status = SessionStatus.DRAINING
        self.end_reason = self.end_reason or reason

    def stats(self) -> dict[str, Any]:
        """Return a stats dict for ``session_closed`` events + DB row."""
        return {
            "session_id": self.session_id.value,
            "tenant_id": self.tenant_id,
            "recording_id": self.recording_id,
            "user_id": self.user_id,
            "started_at": self.started_at.isoformat(),
            "last_chunk_at": self.last_chunk_at.isoformat() if self.last_chunk_at else None,
            "epoch": self.epoch,
            "generation": self.generation,
            "pipeline_run_id": self.pipeline_run_id,
            "ack_seq_high_watermark": self.last_seq,
            "durable_segment_high_watermark": self.durable_segment_high_watermark,
            "seg_confirmed_count": self.seg_confirmed_count,
            "seg_realtime_count": self.seg_realtime_count,
            "bytes_in": self.bytes_in,
            "error_count": self.error_count,
            "end_reason": self.end_reason,
            "status": self.status.value,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _push_realtime(self, delta: ASRDeltaResult) -> None:
        """Append to pending_realtime, enforcing the window cap."""
        self.pending_realtime.append(delta)
        if len(self.pending_realtime) > self.realtime_window:
            # Drop oldest (frontend display window).
            del self.pending_realtime[: len(self.pending_realtime) - self.realtime_window]

    def _attach_confirmed_text(self, text: str) -> None:
        """Attach confirmed ASR text to the most-recent unconfirmed SegmentRecord.

        If no SegmentRecord is pending (because VAD closed the segment before
        this confirmed delta arrived), do nothing — the chunker will pick up
        the confirmed_segments list as-is.
        """
        if not self.confirmed_segments:
            return
        from audio_graphy.core.chunker import SegmentRecord

        last = self.confirmed_segments[-1]
        if not isinstance(last, SegmentRecord):
            return
        if last.transcript:
            # Already had text — append (rare; multiple confirmed in one segment).
            new = SegmentRecord(
                idx=last.idx,
                start_sec=last.start_sec,
                end_sec=last.end_sec,
                transcript=f"{last.transcript} {text}".strip(),
                speaker=last.speaker,
                vad_conf=last.vad_conf,
            )
            self.confirmed_segments[-1] = new
        else:
            new = SegmentRecord(
                idx=last.idx,
                start_sec=last.start_sec,
                end_sec=last.end_sec,
                transcript=text,
                speaker=last.speaker,
                vad_conf=last.vad_conf,
            )
            self.confirmed_segments[-1] = new

    def _drain_confirmed_pairs(self) -> list[dict[str, Any]]:
        """Pair queued ASR confirmations with closed VAD segments in order.

        A ``segment_confirmed`` event is an acknowledgement of a durable
        geometry/text pair, never of ASR text alone.  Keeping an explicit
        cursor also makes replay deterministic when ASR leads VAD.
        """
        from audio_graphy.core.chunker import SegmentRecord

        events: list[dict[str, Any]] = []
        while self.pending_confirmed and self.confirmed_segment_cursor < len(
            self.confirmed_segments
        ):
            segment = self.confirmed_segments[self.confirmed_segment_cursor]
            if not isinstance(segment, SegmentRecord):
                self.confirmed_segment_cursor += 1
                continue

            delta = self.pending_confirmed.pop(0)
            transcript = (
                f"{segment.transcript} {delta.text}".strip() if segment.transcript else delta.text
            )
            paired = SegmentRecord(
                idx=segment.idx,
                start_sec=segment.start_sec,
                end_sec=segment.end_sec,
                transcript=transcript,
                speaker=segment.speaker,
                vad_conf=segment.vad_conf,
            )
            self.confirmed_segments[self.confirmed_segment_cursor] = paired
            self.confirmed_segment_cursor += 1
            self.seg_confirmed_count += 1
            source_seq = delta.seq if delta.seq >= 0 else self.last_seq
            events.append(
                {
                    "type": "segment_confirmed",
                    "session_id": self.session_id.value,
                    "seq": source_seq,
                    "text": delta.text,
                    "is_final": True,
                    "sentence_id": delta.sentence_id,
                    "confirmed_count": self.seg_confirmed_count,
                    "segment": {
                        "idx": paired.idx,
                        "start_sec": paired.start_sec,
                        "end_sec": paired.end_sec,
                        "speaker": paired.speaker,
                        "vad_conf": paired.vad_conf,
                        "transcript": paired.transcript,
                    },
                    "durable": False,
                    "timestamp_ms": int(time.time() * 1000),
                }
            )
        return events

    def _enforce_pcm_cap(self) -> None:
        """Drop oldest PCM samples if pending_speech_pcm exceeds cap."""
        max_bytes = int(self.pcm_buffer_max_sec * PCM_BYTES_PER_SEC_16K_MONO_INT16)
        if len(self.pending_speech_pcm) > max_bytes:
            overflow = len(self.pending_speech_pcm) - max_bytes
            del self.pending_speech_pcm[:overflow]
            logger.info(
                "StreamSession PCM cap enforced: dropped %d bytes (session=%s)",
                overflow,
                self.session_id.value,
            )

    def _enforce_confirmed_cap(self) -> None:
        """Force-flush confirmed_segments if it exceeds the cap.

        The actual flush is done by the caller (StreamingChunker); here we
        just ensure the list doesn't grow unbounded. We keep the most-recent
        N segments.
        """
        if len(self.confirmed_segments) > self.confirmed_flush_threshold * 2:
            # Drop oldest beyond 2× threshold (defensive).
            keep = self.confirmed_flush_threshold * 2
            dropped = len(self.confirmed_segments) - keep
            self.confirmed_segments = self.confirmed_segments[-keep:]
            self.confirmed_segment_cursor = max(
                0,
                self.confirmed_segment_cursor - dropped,
            )


def hash_consent_token(consent_token: str) -> str:
    """sha256(consent_token) — store hash, never the raw token."""
    return hashlib.sha256(consent_token.encode("utf-8")).hexdigest()
