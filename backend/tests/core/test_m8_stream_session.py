"""M8 Phase 4 — StreamSession / StreamingChunker / StreamingRWLock / DeltaGraphUpdater tests.

Covers T4 (StreamSession lifecycle + seq-gap + memory caps),
T6 (StreamingChunker token-budget packing),
T7 (StreamingRWLock concurrency + DeltaGraphUpdater delta detection).

These tests run entirely in mock mode — no real funASR or Silero ONNX.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
from audio_graphy.adapters.protocols import (
    StreamSessionId,
)
from audio_graphy.core.chunker import SegmentRecord
from audio_graphy.core.stream_session import (
    PCM_BYTES_PER_SEC_16K_MONO_INT16,
    SessionStatus,
    StreamSession,
    hash_consent_token,
)


def _make_vad_asr_pair() -> tuple[MockStreamingVADAdapter, MockStreamingASRAdapter]:
    return (
        MockStreamingVADAdapter(latency_ms=0),
        MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0),
    )


def _make_session(
    *,
    vad: MockStreamingVADAdapter | None = None,
    asr: MockStreamingASRAdapter | None = None,
    seq_gap_threshold: int = 3,
    pcm_buffer_max_sec: float = 60.0,
    confirmed_flush_threshold: int = 30,
) -> StreamSession:
    v, a = (vad, asr) if vad is not None and asr is not None else _make_vad_asr_pair()
    return StreamSession(
        session_id=StreamSessionId(value="test-session"),
        tenant_id="default",
        recording_id=1,
        user_id=42,
        consent_token_hash=hash_consent_token("test-consent"),
        vad_adapter=v,
        asr_adapter=a,
        seq_gap_threshold=seq_gap_threshold,
        pcm_buffer_max_sec=pcm_buffer_max_sec,
        confirmed_flush_threshold=confirmed_flush_threshold,
    )


# ============================================================
# T4 — StreamSession
# ============================================================


class TestStreamSessionLifecycle:
    """Verify CREATED → ACTIVE → DRAINING → CLOSED transitions."""

    @pytest.mark.asyncio
    async def test_create_starts_in_created(self) -> None:
        session = _make_session()
        assert session.status == SessionStatus.CREATED

    @pytest.mark.asyncio
    async def test_mark_active_transitions(self) -> None:
        session = _make_session()
        session.mark_active()
        assert session.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_first_chunk_auto_activates(self) -> None:
        session = _make_session()
        events = []
        async for ev in session.on_pcm_chunk(b"\x00" * 1024, seq=0):
            events.append(ev)
        assert session.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_finalize_transitions_to_closed(self) -> None:
        session = _make_session()
        session.mark_active()
        async for _ in session.on_finalize():
            pass
        assert session.status == SessionStatus.CLOSED
        assert session.end_reason == "normal"

    @pytest.mark.asyncio
    async def test_mark_end_with_reason(self) -> None:
        session = _make_session()
        session.mark_end(reason="client_disconnect")
        assert session.status == SessionStatus.CLOSED
        assert session.end_reason == "client_disconnect"

    @pytest.mark.asyncio
    async def test_mark_end_idempotent(self) -> None:
        session = _make_session()
        session.mark_end(reason="error")
        session.mark_end(reason="normal")  # second call ignored
        assert session.end_reason == "error"


class TestStreamSessionOnPCM:
    """Verify on_pcm_chunk processing + server-event emission."""

    @pytest.mark.asyncio
    async def test_bytes_in_accumulates(self) -> None:
        session = _make_session()
        for i in range(5):
            async for _ in session.on_pcm_chunk(b"\x00" * 1024, seq=i):
                pass
        assert session.bytes_in == 5 * 1024

    @pytest.mark.asyncio
    async def test_last_seq_tracks_monotonic(self) -> None:
        session = _make_session()
        for i in range(5):
            async for _ in session.on_pcm_chunk(b"\x00" * 1024, seq=i):
                pass
        assert session.last_seq == 4

    @pytest.mark.asyncio
    async def test_seq_gap_triggers_vad_reset_event(self) -> None:
        session = _make_session(seq_gap_threshold=3)
        events: list[dict[str, Any]] = []
        # Push seq=0..2
        for i in range(3):
            async for _ in session.on_pcm_chunk(b"\x00" * 1024, seq=i):
                pass
        # Now jump seq=10 — gap > 3.
        async for ev in session.on_pcm_chunk(b"\x00" * 1024, seq=10):
            events.append(ev)
        # First event should be vad_reset.
        assert any(e["type"] == "vad_reset" for e in events)
        reset_event = next(e for e in events if e["type"] == "vad_reset")
        assert reset_event["reason"] == "seq_gap"
        assert reset_event["gap"] == 8  # 10 - 2

    @pytest.mark.asyncio
    async def test_seq_within_gap_does_not_reset(self) -> None:
        session = _make_session(seq_gap_threshold=3)
        events: list[dict[str, Any]] = []
        async for _ in session.on_pcm_chunk(b"\x00" * 1024, seq=0):
            pass
        async for ev in session.on_pcm_chunk(b"\x00" * 1024, seq=2):
            events.append(ev)
        assert not any(e["type"] == "vad_reset" for e in events)

    @pytest.mark.asyncio
    async def test_control_reset_emits_vad_reset(self) -> None:
        session = _make_session()
        events = []
        async for ev in session.on_control_reset():
            events.append(ev)
        assert len(events) == 1
        assert events[0]["type"] == "vad_reset"
        assert events[0]["reason"] == "client_request"

    @pytest.mark.asyncio
    async def test_pcm_buffer_cap_enforced(self) -> None:
        session = _make_session(pcm_buffer_max_sec=0.001)  # 1ms cap = 32 bytes
        # Force accumulation by pushing many chunks. The cap should drop oldest.
        for i in range(50):
            async for _ in session.on_pcm_chunk(b"\xff" * 1024, seq=i):
                pass
        # pending_speech_pcm should not exceed cap by much.
        max_bytes = int(0.001 * PCM_BYTES_PER_SEC_16K_MONO_INT16)
        assert len(session.pending_speech_pcm) <= max_bytes + 1024  # slack for one chunk

    @pytest.mark.asyncio
    async def test_confirmed_segments_cap_enforced(self) -> None:
        session = _make_session(confirmed_flush_threshold=2)
        # Inject many SegmentRecord directly (bypass VAD pattern for determinism).
        for i in range(40):
            session.confirmed_segments.append(
                SegmentRecord(
                    idx=i,
                    start_sec=0.0,
                    end_sec=1.0,
                    transcript=f"seg-{i}",
                    speaker=None,
                    vad_conf=1.0,
                )
            )
            session._enforce_confirmed_cap()
        # Should not exceed 2× threshold.
        assert len(session.confirmed_segments) <= 4

    @pytest.mark.asyncio
    async def test_stats_returns_expected_fields(self) -> None:
        session = _make_session()
        session.seg_confirmed_count = 5
        session.bytes_in = 12345
        s = session.stats()
        assert s["seg_confirmed_count"] == 5
        assert s["bytes_in"] == 12345
        assert s["tenant_id"] == "default"
        assert s["recording_id"] == 1

    @pytest.mark.asyncio
    async def test_realtime_event_emitted(self) -> None:
        # Use a tight-interval mock ASR to guarantee a realtime delta.
        asr = MockStreamingASRAdapter(
            connect_latency_ms=0,
            push_latency_ms=0,
            realtime_interval=1,
            confirmed_interval=100,
        )
        await asr.connect(session_id="test-session", tenant_id="default")
        vad = MockStreamingVADAdapter(latency_ms=0)
        session = _make_session(vad=vad, asr=asr)
        events = []
        async for ev in session.on_pcm_chunk(b"\x42" * 1024, seq=0):
            events.append(ev)
        realtime_events = [e for e in events if e["type"] == "realtime_text"]
        assert len(realtime_events) >= 1

    @pytest.mark.asyncio
    async def test_error_count_increments_on_asr_failure(self) -> None:
        class FailingASR:
            async def push_pcm(self, pcm: bytes, *, seq: int):
                raise RuntimeError("synthetic ASR failure")

            async def finalize(self):
                return ()

            async def aclose(self) -> None:
                return

        vad = MockStreamingVADAdapter(latency_ms=0)
        session = _make_session(vad=vad, asr=FailingASR())  # type: ignore[arg-type]
        async for _ in session.on_pcm_chunk(b"\x00" * 1024, seq=0):
            pass
        assert session.error_count == 1


class TestHashConsentToken:
    def test_deterministic_hash(self) -> None:
        h1 = hash_consent_token("abc")
        h2 = hash_consent_token("abc")
        assert h1 == h2

    def test_different_inputs_differ(self) -> None:
        assert hash_consent_token("abc") != hash_consent_token("xyz")

    def test_hash_is_hex_sha256(self) -> None:
        h = hash_consent_token("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ============================================================
# T6 — StreamingChunker
# ============================================================


from audio_graphy.core.streaming_chunker import StreamingChunker  # noqa: E402


class TestStreamingChunker:
    """Verify token-budget packing + flush behaviour."""

    def test_single_short_segment_no_flush(self) -> None:
        chunker = StreamingChunker(token_budget=1200)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="短文本",
            speaker=None,
            vad_conf=1.0,
        )
        result = chunker.push_segment(seg)
        assert result is None
        assert chunker.pending_segment_count == 1

    def test_empty_transcript_skipped(self) -> None:
        chunker = StreamingChunker(token_budget=10)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="",
            speaker=None,
            vad_conf=1.0,
        )
        assert chunker.push_segment(seg) is None
        assert chunker.pending_segment_count == 0

    def test_whitespace_only_transcript_skipped(self) -> None:
        chunker = StreamingChunker(token_budget=10)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="   \n  ",
            speaker=None,
            vad_conf=1.0,
        )
        assert chunker.push_segment(seg) is None

    def test_token_budget_overflow_triggers_flush(self) -> None:
        chunker = StreamingChunker(token_budget=5)  # tiny budget
        # First segment fills buffer (tokenized to >= 5 tokens).
        seg1 = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="artificial intelligence streaming systems",
            speaker=None,
            vad_conf=1.0,
        )
        assert chunker.push_segment(seg1) is None
        # Second segment exceeds budget → flush.
        seg2 = SegmentRecord(
            idx=1,
            start_sec=1.0,
            end_sec=2.0,
            transcript="world more text here now",
            speaker=None,
            vad_conf=1.0,
        )
        chunk = chunker.push_segment(seg2)
        assert chunk is not None
        assert "artificial" in chunk.text
        assert chunk.segment_ids == [0]

    def test_flush_returns_remaining(self) -> None:
        chunker = StreamingChunker(token_budget=1200)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="hello",
            speaker=None,
            vad_conf=1.0,
        )
        chunker.push_segment(seg)
        chunk = chunker.flush()
        assert chunk is not None
        assert chunk.text == "hello"
        assert chunk.segment_ids == [0]
        # Second flush returns None.
        assert chunker.flush() is None

    def test_content_hash_is_sha256(self) -> None:
        import hashlib

        chunker = StreamingChunker(token_budget=5)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="hello world",
            speaker=None,
            vad_conf=1.0,
        )
        chunker.push_segment(seg)
        chunk = chunker.flush()
        assert chunk is not None
        assert chunk.content_hash == hashlib.sha256(b"hello world").hexdigest()

    def test_emitted_count_increments(self) -> None:
        chunker = StreamingChunker(token_budget=5)
        for i in range(5):
            seg = SegmentRecord(
                idx=i,
                start_sec=float(i),
                end_sec=float(i + 1),
                transcript=f"seg-{i}-padding",
                speaker=None,
                vad_conf=1.0,
            )
            chunker.push_segment(seg)
        chunker.flush()
        assert chunker.emitted_count >= 1

    def test_reset_clears_buffer(self) -> None:
        chunker = StreamingChunker(token_budget=1200)
        seg = SegmentRecord(
            idx=0,
            start_sec=0.0,
            end_sec=1.0,
            transcript="hello",
            speaker=None,
            vad_conf=1.0,
        )
        chunker.push_segment(seg)
        chunker.reset()
        assert chunker.pending_segment_count == 0

    def test_invalid_token_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="token_budget"):
            StreamingChunker(token_budget=0)

    def test_multi_segment_chunk_includes_all_ids(self) -> None:
        chunker = StreamingChunker(token_budget=1200)
        for i in range(3):
            seg = SegmentRecord(
                idx=i,
                start_sec=float(i),
                end_sec=float(i + 1),
                transcript=f"seg-{i}",
                speaker=None,
                vad_conf=1.0,
            )
            chunker.push_segment(seg)
        chunk = chunker.flush()
        assert chunk is not None
        assert chunk.segment_ids == [0, 1, 2]
        assert "seg-0" in chunk.text
        assert "seg-2" in chunk.text


# ============================================================
# T7 — StreamingRWLock
# ============================================================


from audio_graphy.core.streaming_rwlock import StreamingRWLock  # noqa: E402


class TestStreamingRWLock:
    """Verify read-write lock semantics."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_readers(self) -> None:
        lock = StreamingRWLock()
        results: list[int] = []

        async def reader(idx: int) -> int:
            async with lock.read_lock():
                await asyncio.sleep(0.01)
                results.append(idx)
                return idx

        await asyncio.gather(*(reader(i) for i in range(5)))
        assert sorted(results) == [0, 1, 2, 3, 4]
        assert lock.reader_count == 0

    @pytest.mark.asyncio
    async def test_writer_excludes_readers(self) -> None:
        lock = StreamingRWLock()
        order: list[str] = []

        async def writer() -> None:
            async with lock.write_lock():
                order.append("write_start")
                await asyncio.sleep(0.02)
                order.append("write_end")

        async def reader() -> None:
            async with lock.read_lock():
                order.append("read")

        # Start writer first so reader must wait.
        await asyncio.gather(writer(), reader())
        # Writer started and ended before reader ran.
        assert order.index("write_end") < order.index("read")

    @pytest.mark.asyncio
    async def test_writer_preference(self) -> None:
        """When a writer is waiting, new readers should block."""
        lock = StreamingRWLock()
        log: list[str] = []

        async def reader(name: str) -> None:
            async with lock.read_lock():
                log.append(f"read:{name}")
                await asyncio.sleep(0.01)

        async def writer(name: str) -> None:
            async with lock.write_lock():
                log.append(f"write:{name}")

        # Acquire read lock, then queue writer + second reader.
        async def scenario() -> None:
            t1 = asyncio.create_task(reader("first"))
            await asyncio.sleep(0.001)  # ensure first reader acquired
            t2 = asyncio.create_task(writer("w"))
            t3 = asyncio.create_task(reader("second"))
            await asyncio.sleep(0.001)
            # Wait for all.
            await asyncio.gather(t1, t2, t3)

        await scenario()
        # First reader must finish before writer; writer before second reader.
        assert log.index("read:first") < log.index("write:w") < log.index("read:second")

    @pytest.mark.asyncio
    async def test_no_writer_active_after_release(self) -> None:
        lock = StreamingRWLock()
        async with lock.write_lock():
            pass
        assert not lock.writer_active
        assert lock.reader_count == 0

    @pytest.mark.asyncio
    async def test_release_write_notifies_all(self) -> None:
        lock = StreamingRWLock()
        started = asyncio.Event()

        async def writer() -> None:
            async with lock.write_lock():
                started.set()
                await asyncio.sleep(0.05)

        async def reader() -> None:
            await started.wait()
            async with lock.read_lock():
                return True

        t_w = asyncio.create_task(writer())
        t_r = asyncio.create_task(reader())
        result = await asyncio.wait_for(asyncio.gather(t_w, t_r), timeout=1.0)
        assert result[1] is True


# ============================================================
# T7 — DeltaGraphUpdater (delta detection only — full pipeline needs DB session)
# ============================================================


from audio_graphy.core.delta_graph_updater import DeltaUpdateReport  # noqa: E402


class TestDeltaUpdateReport:
    """Verify the report dataclass."""

    def test_default_construction(self) -> None:
        r = DeltaUpdateReport(
            chunk_id=1,
            skipped_by_hash=False,
            new_entities=2,
            merged_entities=1,
            new_edges=3,
            ambiguous_edges=1,
            speaker_links=0,
            extraction_ms=10.0,
            merge_ms=5.0,
            persist_ms=2.0,
        )
        assert r.chunk_id == 1
        assert r.new_edges == 3
        assert r.ambiguous_edges == 1

    def test_skipped_report_shape(self) -> None:
        r = DeltaUpdateReport(
            chunk_id=99,
            skipped_by_hash=True,
            new_entities=0,
            merged_entities=0,
            new_edges=0,
            ambiguous_edges=0,
            speaker_links=0,
            extraction_ms=0.0,
            merge_ms=0.0,
            persist_ms=0.0,
        )
        assert r.skipped_by_hash is True


class TestDeltaGraphUpdaterEdgeTagging:
    """Verify edge-confidence tagging via direct method tests."""

    def _make_updater(self) -> Any:
        # Bypass __init__ — we only test the static-ish helpers.
        from audio_graphy.core.delta_graph_updater import DeltaGraphUpdater

        updater = DeltaGraphUpdater.__new__(DeltaGraphUpdater)
        return updater

    def test_extracted_relation_with_remap_becomes_ambiguous(self) -> None:
        from audio_graphy.core.extractor import ExtractedRelation

        updater = self._make_updater()
        rel = ExtractedRelation(
            source_name="raw_a",
            target_name="raw_b",
            relation="r",
            description="d",
            weight=1.0,
            confidence="EXTRACTED",
            chunk_id=1,
            recording_id=1,
        )
        # Both endpoints remapped → AMBIGUOUS.
        name_remap = {"raw_a": "canonical_a", "raw_b": "canonical_b"}
        tagged = updater._tag_edges([rel], name_remap, [])
        assert tagged[0][1] == "AMBIGUOUS"

    def test_extracted_relation_without_remap_stays_extracted(self) -> None:
        from audio_graphy.core.extractor import ExtractedRelation

        updater = self._make_updater()
        rel = ExtractedRelation(
            source_name="a",
            target_name="b",
            relation="r",
            description="d",
            weight=1.0,
            confidence="EXTRACTED",
            chunk_id=1,
            recording_id=1,
        )
        tagged = updater._tag_edges([rel], {}, [])
        assert tagged[0][1] == "EXTRACTED"

    def test_inferred_relation_stays_inferred(self) -> None:
        from audio_graphy.core.extractor import ExtractedRelation

        updater = self._make_updater()
        rel = ExtractedRelation(
            source_name="a",
            target_name="b",
            relation="r",
            description="d",
            weight=1.0,
            confidence="INFERRED",
            chunk_id=1,
            recording_id=1,
        )
        tagged = updater._tag_edges([rel], {}, [])
        assert tagged[0][1] == "INFERRED"
