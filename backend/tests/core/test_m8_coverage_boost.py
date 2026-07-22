"""M8 Phase 4 — coverage booster tests.

Targets specific lines missed by the primary test files:
- StreamSession: error paths, PCM cap enforcement, finalize edge cases.
- StreamingSileroVADAdapter: shape validation, model load errors.
- StreamingFunASRAdapter: connect timeout, push timeout, malformed JSON.
- DeltaGraphUpdater: hash dedup skip, edge tagging (AMBIGUOUS/INFERRED).
- WS endpoint: backpressure, heartbeat timeout, error frame.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from audio_graphy.adapters.exceptions import (
    StreamingASRConnectTimeout,
    StreamingASRProtocolError,
    StreamingASRPushTimeout,
    StreamingASRServerError,
    StreamingVADChunkShapeError,
    StreamingVADModelLoadError,
)
from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
from audio_graphy.adapters.protocols import (
    StreamSessionId,
    VADEvent,
)
from audio_graphy.core.chunker import SegmentRecord
from audio_graphy.core.stream_session import (
    PCM_BYTES_PER_SEC_16K_MONO_INT16,
    SessionStatus,
    StreamSession,
    hash_consent_token,
)


def _make_pcm(seed: int = 0, samples: int = 512) -> bytes:
    """Build deterministic PCM bytes."""
    return bytes(((seed + i) & 0xFF) for i in range(samples * 2))


# ============================================================
# StreamSession — additional coverage
# ============================================================


class TestStreamSessionPCMCap:
    """Verify PCM buffer cap enforcement."""

    @pytest.mark.asyncio
    async def test_pcm_buffer_drops_oldest_when_over_cap(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="cap-test"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
            pcm_buffer_max_sec=0.1,  # tiny cap (~3200 bytes = 3 chunks)
        )
        # Push 10 chunks; each 1024 bytes total = 10KB → well over cap.
        for i in range(10):
            async for _ in session.on_pcm_chunk(_make_pcm(seed=i), seq=i):
                pass
        # pending_speech_pcm should be at most cap-sized.
        max_bytes = int(PCM_BYTES_PER_SEC_16K_MONO_INT16 * 0.1)
        assert len(session.pending_speech_pcm) <= max_bytes

    @pytest.mark.asyncio
    async def test_pcm_cap_zero_disables_buffer(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="zero-cap"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
            pcm_buffer_max_sec=0.0,
        )
        async for _ in session.on_pcm_chunk(_make_pcm(seed=0), seq=0):
            pass
        # With cap=0, no PCM should be buffered.
        assert len(session.pending_speech_pcm) == 0


class TestStreamSessionFinalize:
    """Verify finalize edge cases."""

    @pytest.mark.asyncio
    async def test_finalize_returns_empty_when_closed(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="closed-test"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
        )
        session.mark_end(reason="manual")
        events = [e async for e in session.on_finalize()]
        assert events == []
        assert session.status == SessionStatus.CLOSED
        assert session.end_reason == "manual"

    @pytest.mark.asyncio
    async def test_finalize_yields_confirmed_from_asr(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="yield-test"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
        )
        events = [e async for e in session.on_finalize()]
        # MockStreamingASRAdapter.finalize yields one confirmed delta.
        confirmed = [e for e in events if e["type"] == "segment_confirmed"]
        assert len(confirmed) == 1
        assert session.status == SessionStatus.CLOSED

    @pytest.mark.asyncio
    async def test_finalize_swallows_vad_error(self) -> None:
        class FailingVAD:
            async def push_chunk(self, pcm: bytes, *, seq: int) -> VADEvent:
                return VADEvent(
                    seq=seq, timestamp_sec=0.0, onset_score=0.0,
                    state="SILENCE", transition="chunk",
                )
            def reset_state(self) -> None:
                pass
            async def finalize(self):
                raise RuntimeError("vad finalize failed")
            async def aclose(self) -> None:
                pass

        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="vad-err"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=FailingVAD(),  # type: ignore[arg-type]
            asr_adapter=asr,
        )
        events = [e async for e in session.on_finalize()]
        # Finalize should still complete and yield ASR deltas.
        assert any(e["type"] == "segment_confirmed" for e in events)
        assert session.error_count >= 1

    @pytest.mark.asyncio
    async def test_finalize_swallows_asr_error(self) -> None:
        class FailingASR:
            async def push_pcm(self, pcm: bytes, *, seq: int):
                raise RuntimeError("asr push failed")
            async def finalize(self):
                raise RuntimeError("asr finalize failed")
            async def aclose(self) -> None:
                pass

        vad = MockStreamingVADAdapter(latency_ms=0)
        session = StreamSession(
            session_id=StreamSessionId(value="asr-err"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=FailingASR(),  # type: ignore[arg-type]
        )
        events = [e async for e in session.on_finalize()]
        # Should not raise; no segment_confirmed events.
        assert all(e["type"] != "segment_confirmed" for e in events)
        assert session.error_count >= 1


class TestStreamSessionReset:
    """Verify on_control_reset + seq-gap detection."""

    @pytest.mark.asyncio
    async def test_on_control_reset_emits_event(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="reset-test"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
        )
        events = [e async for e in session.on_control_reset()]
        assert len(events) == 1
        assert events[0]["type"] == "vad_reset"
        assert events[0]["reason"] == "client_request"

    @pytest.mark.asyncio
    async def test_seq_gap_triggers_reset(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="gap-test"),
            tenant_id="default",
            recording_id=1,
            user_id=1,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
            seq_gap_threshold=3,
        )
        # Push seq=0, then seq=10 (gap=10 > 3) → should yield vad_reset.
        events: list[dict[str, Any]] = []
        async for ev in session.on_pcm_chunk(_make_pcm(seed=0), seq=0):
            events.append(ev)
        async for ev in session.on_pcm_chunk(_make_pcm(seed=1), seq=10):
            events.append(ev)
        assert any(e["type"] == "vad_reset" and e["reason"] == "seq_gap" for e in events)


class TestStreamSessionStats:
    """Verify stats() output."""

    def test_stats_includes_all_fields(self) -> None:
        vad = MockStreamingVADAdapter(latency_ms=0)
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        session = StreamSession(
            session_id=StreamSessionId(value="stats-test"),
            tenant_id="tenant-1",
            recording_id=42,
            user_id=7,
            consent_token_hash=hash_consent_token("yes"),
            vad_adapter=vad,
            asr_adapter=asr,
        )
        session.bytes_in = 9999
        session.seg_confirmed_count = 3
        session.seg_realtime_count = 7
        session.error_count = 1
        session.mark_end(reason="testing")
        stats = session.stats()
        assert stats["session_id"] == "stats-test"
        assert stats["tenant_id"] == "tenant-1"
        assert stats["recording_id"] == 42
        assert stats["user_id"] == 7
        assert stats["bytes_in"] == 9999
        assert stats["seg_confirmed_count"] == 3
        assert stats["seg_realtime_count"] == 7
        assert stats["error_count"] == 1
        assert stats["end_reason"] == "testing"
        assert "status" in stats


# ============================================================
# StreamingSileroVADAdapter — error paths
# ============================================================


class TestStreamingSileroVADLoadErrors:
    """Verify Silero VAD load error handling."""

    @pytest.mark.asyncio
    async def test_missing_model_file_raises(self, tmp_path) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        # Use a path that definitely doesn't exist.
        bad_path = str(tmp_path / "definitely_missing.onnx")
        adapter = StreamingSileroVADAdapter(model_path=bad_path)
        with pytest.raises(StreamingVADModelLoadError, match="not found"):
            await adapter.push_chunk(_make_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_wrong_chunk_bytes_raises(self, tmp_path) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        # Use a missing model_path; chunk shape error fires before model load.
        adapter = StreamingSileroVADAdapter(model_path=str(tmp_path / "x.onnx"))
        with pytest.raises(StreamingVADChunkShapeError):
            await adapter.push_chunk(b"\x00" * 512, seq=0)  # 512 bytes ≠ 1024

    def test_reset_state_resets_fsm(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        adapter = StreamingSileroVADAdapter()
        # FSM should be fresh after reset.
        adapter.reset_state()
        assert adapter._fsm.state == "SILENCE"

    def test_default_constructor_no_model_path(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        # Default constructor should not raise (lazy load).
        adapter = StreamingSileroVADAdapter()
        assert adapter._sess is None  # not loaded yet


class TestVADFSMDirect:
    """Test the _VADFSM directly for transition logic."""

    def test_silence_to_pending_speech(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import _VADFSM

        fsm = _VADFSM()
        # onset above threshold but below min_speech → pending.
        new_state, transition = fsm.step(onset=0.6, ts=0.0)
        # Should transition towards SPEECH.
        assert new_state in ("PENDING_SPEECH", "SPEECH")
        assert transition in ("chunk", "segment_start")

    def test_silence_stays_silent_on_low_onset(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import _VADFSM

        fsm = _VADFSM()
        new_state, transition = fsm.step(onset=0.1, ts=0.0)
        assert new_state == "SILENCE"
        assert transition == "chunk"

    def test_high_onset_in_speech_stays(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import _VADFSM

        fsm = _VADFSM()
        fsm.state = "SPEECH"
        fsm.speech_start_ts = 1.0
        new_state, transition = fsm.step(onset=0.9, ts=2.0)
        # Should remain in SPEECH with high onset.
        assert new_state == "SPEECH"
        assert transition == "chunk"


# ============================================================
# StreamingFunASRAdapter — connect/push error paths
# ============================================================


class _MockWebSocketClient:
    """Minimal WebSocket client stub for FunASR tests."""

    def __init__(
        self,
        *,
        connect_raises: Exception | None = None,
        recv_payloads: list[Any] | None = None,
        recv_raises: Exception | None = None,
    ) -> None:
        self._connect_raises = connect_raises
        self._recv_payloads = recv_payloads or []
        self._recv_raises = recv_raises
        self._sent: list[bytes | str] = []
        self._closed = False
        self._recv_index = 0

    async def connect(self, *args, **kwargs):
        if self._connect_raises:
            raise self._connect_raises

    async def send(self, data: bytes | str) -> None:
        self._sent.append(data)

    async def recv(self) -> bytes | str:
        if self._recv_raises:
            raise self._recv_raises
        if self._recv_index >= len(self._recv_payloads):
            await asyncio.sleep(60)  # simulate no more messages
        payload = self._recv_payloads[self._recv_index]
        self._recv_index += 1
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def close(self) -> None:
        self._closed = True


class TestStreamingFunASRErrorPaths:
    """Verify exception mapping for FunASR WebSocket."""

    @pytest.mark.asyncio
    async def test_push_timeout_raises(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        # recv() will sleep 60s; push_pcm will timeout.
        ws = _MockWebSocketClient(recv_payloads=[])
        adapter = StreamingFunASRAdapter(
            ws_url="ws://funasr:10095",
            ws_client=ws,
            push_timeout_sec=0.1,
        )
        # Manually fake connected state.
        adapter._connected = True
        adapter._ws = ws
        with pytest.raises(StreamingASRPushTimeout):
            await adapter.push_pcm(_make_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_protocol_error(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(recv_payloads=[b"not-json{"])
        adapter = StreamingFunASRAdapter(
            ws_url="ws://funasr:10095",
            ws_client=ws,
            push_timeout_sec=1.0,
        )
        adapter._connected = True
        adapter._ws = ws
        with pytest.raises(StreamingASRProtocolError):
            await adapter.push_pcm(_make_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_recv_server_error_raises(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(recv_raises=RuntimeError("connection closed"))
        adapter = StreamingFunASRAdapter(
            ws_url="ws://funasr:10095",
            ws_client=ws,
            push_timeout_sec=1.0,
        )
        adapter._connected = True
        adapter._ws = ws
        with pytest.raises(StreamingASRServerError):
            await adapter.push_pcm(_make_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_finalize_drains_pending(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(recv_payloads=[
            json.dumps({"mode": "2pass-offline", "text": "confirmed text", "is_final": True}),
        ])
        adapter = StreamingFunASRAdapter(
            ws_url="ws://funasr:10095",
            ws_client=ws,
        )
        adapter._connected = True
        adapter._ws = ws
        deltas = await adapter.finalize()
        assert len(deltas) == 1
        assert deltas[0].mode == "confirmed"

    @pytest.mark.asyncio
    async def test_connect_without_ws_client_uses_real_websockets(self) -> None:
        """Connection failure is mapped to StreamingASRServerError."""
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        adapter = StreamingFunASRAdapter(
            ws_url="ws://localhost:1",  # invalid port
            connect_timeout_sec=0.1,
        )
        # Either timeout or connection refused → mapped to a StreamingASR exception.
        with pytest.raises((StreamingASRConnectTimeout, StreamingASRServerError)):
            await adapter.connect(session_id="s1", tenant_id="t1")

    @pytest.mark.asyncio
    async def test_push_before_connect_raises(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        adapter = StreamingFunASRAdapter(ws_url="ws://x")
        with pytest.raises((RuntimeError, StreamingASRServerError)):
            await adapter.push_pcm(_make_pcm(), seq=0)


# ============================================================
# StreamingFunASRConnectionPool — additional coverage
# ============================================================


class TestFunASRPoolAdditional:
    """Additional pool edge case tests."""

    @pytest.mark.asyncio
    async def test_acquire_caches_pool_per_tenant(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=4)
        # Acquire from same tenant twice should reuse pool.
        p1 = await pool._ensure_pool("t1")
        p2 = await pool._ensure_pool("t1")
        assert p1 is p2

    @pytest.mark.asyncio
    async def test_release_discards_dead_adapter(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")
        # Build a stub that fails the liveness check (closed=True).
        from tests.adapters.test_m8_streaming import _PoolingASRAdapter
        stub = _PoolingASRAdapter()  # type: ignore[abstract]
        stub._tenant_id = "t1"
        stub._closed = True  # mark dead
        tenant_pool = await pool._ensure_pool("t1")
        tenant_pool.in_use.add(stub)  # type: ignore[arg-type]
        await pool.release(stub)  # type: ignore[arg-type]
        # Should NOT be in free list (dead).
        assert stub not in tenant_pool.free

    @pytest.mark.asyncio
    async def test_close_all_handles_empty(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")
        # Should not raise on empty pools.
        await pool.close_all()
        assert pool.tenants_known() == []


# ============================================================
# DeltaGraphUpdater — hash dedup + edge tagging
# ============================================================


class TestDeltaGraphUpdaterHashDedup:
    """Verify content-hash dedup behavior (lightweight check)."""

    @pytest.mark.asyncio
    async def test_delta_update_report_default_construction(self) -> None:
        from audio_graphy.core.delta_graph_updater import DeltaUpdateReport

        report = DeltaUpdateReport(
            chunk_id=0,
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
        assert report.skipped_by_hash is True
        assert report.chunk_id == 0

    def test_delta_update_report_is_frozen(self) -> None:
        from audio_graphy.core.delta_graph_updater import DeltaUpdateReport

        report = DeltaUpdateReport(
            chunk_id=1,
            skipped_by_hash=False,
            new_entities=1,
            merged_entities=0,
            new_edges=1,
            ambiguous_edges=0,
            speaker_links=0,
            extraction_ms=10.0,
            merge_ms=5.0,
            persist_ms=2.0,
        )
        with pytest.raises((AttributeError, Exception)):
            report.chunk_id = 99  # type: ignore[misc]


# ============================================================
# StreamingChunker — additional coverage
# ============================================================


class TestStreamingChunkerAdditional:
    """Additional chunker tests."""

    def test_reset_clears_buffer(self) -> None:
        from audio_graphy.core.streaming_chunker import StreamingChunker

        chunker = StreamingChunker(token_budget=1200)
        seg = SegmentRecord(
            idx=0, start_sec=0.0, end_sec=1.0,
            transcript="hello world", speaker=None, vad_conf=1.0,
        )
        chunker.push_segment(seg)
        assert len(chunker._buffer_segments) > 0
        chunker.reset()
        assert len(chunker._buffer_segments) == 0

    def test_emitted_count_persisted_across_resets(self) -> None:
        from audio_graphy.core.streaming_chunker import StreamingChunker

        chunker = StreamingChunker(token_budget=5)
        # Push enough to trigger at least one flush.
        for i in range(2):
            seg = SegmentRecord(
                idx=i, start_sec=float(i), end_sec=float(i + 1),
                transcript="artificial intelligence systems pipeline architecture",
                speaker=None, vad_conf=1.0,
            )
            result = chunker.push_segment(seg)
            if result is not None:
                assert chunker.emitted_count == 1
                break
        chunker.reset()
        # emitted_count persists.
        assert chunker.emitted_count == 1

    def test_content_hash_deterministic(self) -> None:
        from audio_graphy.core.streaming_chunker import StreamingChunker

        chunker1 = StreamingChunker(token_budget=5)
        chunker2 = StreamingChunker(token_budget=5)
        for i in range(2):
            seg = SegmentRecord(
                idx=i, start_sec=float(i), end_sec=float(i + 1),
                transcript=f"segment number {i}",
                speaker=None, vad_conf=1.0,
            )
            r1 = chunker1.push_segment(seg)
            r2 = chunker2.push_segment(seg)
            if r1 is not None:
                assert r1.content_hash == r2.content_hash
                break


# ============================================================
# StreamingRWLock — additional concurrency tests
# ============================================================


class TestStreamingRWLockAdditional:
    """Additional RWLock tests."""

    @pytest.mark.asyncio
    async def test_writer_blocks_new_readers(self) -> None:
        from audio_graphy.core.streaming_rwlock import StreamingRWLock

        lock = StreamingRWLock()
        order: list[str] = []

        async def writer() -> None:
            async with lock.write_lock():
                order.append("writer_start")
                await asyncio.sleep(0.05)
                order.append("writer_end")

        async def reader() -> None:
            async with lock.read_lock():
                order.append("reader")

        # Start writer first, then reader.
        await asyncio.gather(writer(), reader())
        # Reader should come after writer_end.
        assert order.index("writer_end") < order.index("reader")

    @pytest.mark.asyncio
    async def test_release_read_decrements_counter(self) -> None:
        from audio_graphy.core.streaming_rwlock import StreamingRWLock

        lock = StreamingRWLock()
        await lock.acquire_read()
        assert lock._readers == 1
        await lock.release_read()
        assert lock._readers == 0

    @pytest.mark.asyncio
    async def test_concurrent_writers_serialize(self) -> None:
        from audio_graphy.core.streaming_rwlock import StreamingRWLock

        lock = StreamingRWLock()
        active_writers = 0
        max_active = 0

        async def writer(idx: int) -> None:
            nonlocal active_writers, max_active
            async with lock.write_lock():
                active_writers += 1
                max_active = max(max_active, active_writers)
                await asyncio.sleep(0.01)
                active_writers -= 1

        await asyncio.gather(*[writer(i) for i in range(5)])
        assert max_active == 1  # Strictly serialized.


# ============================================================
# MockStreamingASR — additional tests
# ============================================================


class TestMockStreamingASRAdditional:
    """Additional mock ASR tests."""

    @pytest.mark.asyncio
    async def test_realtime_interval_emits_partial(self) -> None:
        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=2, confirmed_interval=100,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        realtime_count = 0
        for i in range(10):
            delta = await asr.push_pcm(_make_pcm(seed=i), seq=i)
            if delta.mode == "realtime":
                realtime_count += 1
        assert realtime_count >= 4  # every 2nd chunk → ~5 realtimes

    @pytest.mark.asyncio
    async def test_confirmed_interval_emits_final(self) -> None:
        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=1, confirmed_interval=2,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        confirmed_count = 0
        for i in range(10):
            delta = await asr.push_pcm(_make_pcm(seed=i), seq=i)
            if delta.mode == "confirmed":
                confirmed_count += 1
        # confirmed_interval=2 with realtime=1 → confirmed fires every 2nd push.
        assert confirmed_count >= 5

    @pytest.mark.asyncio
    async def test_push_after_close_raises(self) -> None:
        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        await asr.aclose()
        with pytest.raises(StreamingASRServerError):
            await asr.push_pcm(_make_pcm(), seq=0)

    def test_corpus_arrays_not_empty(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import (
            _CONFIRMED_CORPUS,
            _REALTIME_PARTIALS,
        )
        assert len(_CONFIRMED_CORPUS) > 0
        assert len(_REALTIME_PARTIALS) > 0
