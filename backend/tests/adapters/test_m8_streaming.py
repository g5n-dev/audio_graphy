"""M8 Phase 4 — streaming adapter + protocol + exception tests.

Covers T1 (protocols / exceptions), T2 (streaming VAD real + mock),
T3 (streaming ASR real + mock + pool).

Tests run in mock mode only — real-mode tests need onnxruntime + a fake
funASR WebSocket server (kept in a separate round / integration suite).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence

import pytest

# ============================================================
# T1 — Protocols + dataclasses + exceptions
# ============================================================


class TestStreamingProtocols:
    """Verify new Protocol + dataclass contracts."""

    def test_vad_event_frozen(self) -> None:
        from audio_graphy.adapters.protocols import VADEvent

        ev = VADEvent(
            seq=1,
            timestamp_sec=1.0,
            onset_score=0.5,
            state="SILENCE",
            transition="chunk",
        )
        assert ev.seq == 1
        assert ev.segment is None
        assert ev.reset is False
        with pytest.raises(AttributeError):  # frozen dataclass
            ev.seq = 2  # type: ignore[misc]

    def test_asr_delta_result_defaults(self) -> None:
        from audio_graphy.adapters.protocols import ASRDeltaResult

        d = ASRDeltaResult(
            seq=1, mode="realtime", text="hello", is_final=False, sentence_id=0,
        )
        assert d.confidence == 0.95

    def test_stream_session_id_opaque(self) -> None:
        from audio_graphy.adapters.protocols import StreamSessionId

        sid = StreamSessionId(value="abc-123")
        assert sid.value == "abc-123"

    def test_streaming_vad_protocol_runtime_checkable(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
        from audio_graphy.adapters.protocols import StreamingVADAdapter

        assert isinstance(MockStreamingVADAdapter(), StreamingVADAdapter)

    def test_streaming_asr_protocol_runtime_checkable(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
        from audio_graphy.adapters.protocols import StreamingASRAdapter

        assert isinstance(MockStreamingASRAdapter(), StreamingASRAdapter)

    def test_real_streaming_vad_satisfies_protocol(self) -> None:
        from audio_graphy.adapters.protocols import StreamingVADAdapter
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        assert isinstance(StreamingSileroVADAdapter(), StreamingVADAdapter)

    def test_real_streaming_asr_satisfies_protocol(self) -> None:
        from audio_graphy.adapters.protocols import StreamingASRAdapter
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        assert isinstance(
            StreamingFunASRAdapter(ws_url="ws://example"), StreamingASRAdapter
        )


class TestStreamingExceptions:
    """Verify exception hierarchy + mixins + module paths."""

    def test_vad_chunk_shape_error_inherits(self) -> None:
        from audio_graphy.adapters.exceptions import (
            RequestErrorMixin,
            StreamingVADAdapterError,
            StreamingVADChunkShapeError,
        )

        exc = StreamingVADChunkShapeError("bad shape")
        assert isinstance(exc, StreamingVADAdapterError)
        assert isinstance(exc, RequestErrorMixin)
        assert exc.url is None
        assert exc.status_code is None

    def test_vad_model_load_error_is_server(self) -> None:
        from audio_graphy.adapters.exceptions import (
            ServerErrorMixin,
            StreamingVADModelLoadError,
        )

        exc = StreamingVADModelLoadError("missing model", url="/models/x.onnx")
        assert isinstance(exc, ServerErrorMixin)
        assert exc.url == "/models/x.onnx"

    def test_asr_timeout_mixins(self) -> None:
        from audio_graphy.adapters.exceptions import (
            StreamingASRConnectTimeout,
            StreamingASRPushTimeout,
            TimeoutErrorMixin,
        )

        for cls in (StreamingASRConnectTimeout, StreamingASRPushTimeout):
            exc = cls("timeout")
            assert isinstance(exc, TimeoutErrorMixin)

    def test_asr_protocol_error_inherits(self) -> None:
        from audio_graphy.adapters.exceptions import (
            ServerErrorMixin,
            StreamingASRProtocolError,
        )

        assert isinstance(StreamingASRProtocolError("bad"), ServerErrorMixin)

    def test_websocket_session_error_carries_session_id(self) -> None:
        from audio_graphy.adapters.exceptions import (
            WebSocketBackpressureOverflow,
            WebSocketSessionError,
        )

        exc = WebSocketBackpressureOverflow("overflow", session_id="abc")
        assert isinstance(exc, WebSocketSessionError)
        assert exc.session_id == "abc"

    def test_exception_module_path_stable(self) -> None:
        from audio_graphy.adapters.exceptions import (
            StreamingASRAuthError,
            StreamingVADChunkShapeError,
            WebSocketSessionError,
        )

        assert StreamingVADChunkShapeError.__module__ == "audio_graphy.adapters.exceptions"
        assert StreamingASRAuthError.__module__ == "audio_graphy.adapters.exceptions"
        assert WebSocketSessionError.__module__ == "audio_graphy.adapters.exceptions"


# ============================================================
# T2 — MockStreamingVADAdapter behaviour
# ============================================================


def _make_pcm(seed: int = 0, size: int = 1024) -> bytes:
    """Deterministic PCM chunk of the requested size."""
    return bytes((i + seed) & 0xFF for i in range(size))


class TestMockStreamingVAD:
    """Verify MockStreamingVADAdapter FSM + determinism."""

    @pytest.mark.asyncio
    async def test_push_chunk_returns_vad_event(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
        from audio_graphy.adapters.protocols import VADEvent

        vad = MockStreamingVADAdapter(latency_ms=0)
        ev = await vad.push_chunk(_make_pcm(), seq=1)
        assert isinstance(ev, VADEvent)
        assert ev.seq == 1
        assert ev.state in ("SILENCE", "PENDING_SPEECH", "SPEECH", "PENDING_SILENCE")
        assert ev.transition in ("chunk", "segment_start", "segment_end")

    @pytest.mark.asyncio
    async def test_push_chunk_wrong_size_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingVADChunkShapeError
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0)
        with pytest.raises(StreamingVADChunkShapeError):
            await vad.push_chunk(b"\x00" * 512, seq=1)  # half-size

    @pytest.mark.asyncio
    async def test_chunk_count_pattern_emits_segments(self) -> None:
        """Verify that the chunk-count pattern emits ≥1 segment_start + segment_end."""
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0)
        events = []
        for i in range(220):  # > 200 → at least one segment_end cycle
            ev = await vad.push_chunk(_make_pcm(seed=i), seq=i)
            events.append(ev)

        starts = [e for e in events if e.transition == "segment_start"]
        ends = [e for e in events if e.transition == "segment_end"]
        assert len(starts) >= 1
        assert len(ends) >= 1
        # segment_end must come AFTER segment_start in seq order.
        assert ends[0].seq > starts[0].seq

    @pytest.mark.asyncio
    async def test_reset_state_clears_buffer(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0)
        # Trigger a segment_start (chunk_count becomes 50, 50%50==0).
        for i in range(50):
            await vad.push_chunk(_make_pcm(seed=i), seq=i)
        vad.reset_state()
        # After reset, chunk_count=0; push 50 chunks to reach the next segment_start.
        for i in range(50):
            ev = await vad.push_chunk(_make_pcm(seed=i + 100), seq=i + 100)
        assert ev.transition == "segment_start"
        assert ev.reset is False

    @pytest.mark.asyncio
    async def test_finalize_flushes_in_progress(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0)
        # Trigger segment_start but NOT segment_end.
        for i in range(60):
            await vad.push_chunk(_make_pcm(seed=i), seq=i)
        trailing = await vad.finalize()
        assert len(trailing) == 1  # in-progress segment flushed

    @pytest.mark.asyncio
    async def test_deterministic_onset_same_input(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad1 = MockStreamingVADAdapter(latency_ms=0)
        vad2 = MockStreamingVADAdapter(latency_ms=0)
        ev1 = await vad1.push_chunk(_make_pcm(seed=42), seq=0)
        ev2 = await vad2.push_chunk(_make_pcm(seed=42), seq=0)
        assert ev1.onset_score == ev2.onset_score

    @pytest.mark.asyncio
    async def test_flaky_raises_runtime_error(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0, flaky=True)
        # Push 100 chunks (chunk_count becomes 100). Flaky check at top of push
        # fires when chunk_count % 100 == 0 — i.e., on chunk 101.
        for i in range(100):
            await vad.push_chunk(_make_pcm(seed=i), seq=i)
        with pytest.raises(RuntimeError, match="flaky"):
            await vad.push_chunk(_make_pcm(seed=100), seq=100)

    @pytest.mark.asyncio
    async def test_invalid_chunk_samples_rejected(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        with pytest.raises(ValueError, match="chunk_samples must be 512"):
            MockStreamingVADAdapter(chunk_samples=256)

    @pytest.mark.asyncio
    async def test_aclose_is_noop(self) -> None:
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter

        vad = MockStreamingVADAdapter(latency_ms=0)
        await vad.aclose()  # should not raise


# ============================================================
# T2 — StreamingSileroVADAdapter (without ONNX — shape errors only)
# ============================================================


class TestStreamingSileroVADShapeValidation:
    """Real VAD adapter shape validation works without onnxruntime installed."""

    @pytest.mark.asyncio
    async def test_wrong_chunk_size_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingVADChunkShapeError
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        vad = StreamingSileroVADAdapter(model_path="/nonexistent.onnx")
        with pytest.raises(StreamingVADChunkShapeError):
            await vad.push_chunk(b"\x00" * 512, seq=1)

    @pytest.mark.asyncio
    async def test_missing_model_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingVADModelLoadError
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        vad = StreamingSileroVADAdapter(model_path="/nonexistent/silero.onnx")
        with pytest.raises(StreamingVADModelLoadError):
            await vad.push_chunk(b"\x00" * 1024, seq=1)

    @pytest.mark.asyncio
    async def test_reset_state_no_op_when_no_session(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        vad = StreamingSileroVADAdapter()
        vad.reset_state()  # should not raise even before any push
        assert vad._state is not None

    @pytest.mark.asyncio
    async def test_finalize_empty_returns_empty_tuple(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        vad = StreamingSileroVADAdapter()
        result = await vad.finalize()
        assert result == ()

    @pytest.mark.asyncio
    async def test_aclose_resets_session(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        vad = StreamingSileroVADAdapter()
        await vad.aclose()
        assert vad._sess is None


# ============================================================
# T2 — VAD FSM unit tests (no ONNX)
# ============================================================


class TestVADFSM:
    """Unit-test the 4-state FSM directly (no model inference)."""

    def test_silence_to_pending_speech(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SPEECH,
            _VADFSM,
        )

        fsm = _VADFSM()
        new_state, transition = fsm.step(onset=0.7, ts=0.1)
        assert new_state == _PENDING_SPEECH
        assert transition == "chunk"

    def test_silence_stays_silence_on_low_onset(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _SILENCE,
            _VADFSM,
        )

        fsm = _VADFSM()
        new_state, _ = fsm.step(onset=0.2, ts=0.1)
        assert new_state == _SILENCE

    def test_pending_speech_to_speech_after_min_speech(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SPEECH,
            _SPEECH,
            _VADFSM,
        )

        fsm = _VADFSM(min_speech_sec=0.05)
        fsm.state = _PENDING_SPEECH
        fsm.pending_start_ts = 0.0
        new_state, transition = fsm.step(onset=0.8, ts=0.1)
        assert new_state == _SPEECH
        assert transition == "segment_start"

    def test_pending_speech_falls_back_to_silence(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SPEECH,
            _SILENCE,
            _VADFSM,
        )

        fsm = _VADFSM()
        fsm.state = _PENDING_SPEECH
        fsm.pending_start_ts = 0.0
        new_state, _ = fsm.step(onset=0.1, ts=0.5)
        assert new_state == _SILENCE

    def test_speech_to_pending_silence(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SILENCE,
            _SPEECH,
            _VADFSM,
        )

        fsm = _VADFSM()
        fsm.state = _SPEECH
        new_state, _ = fsm.step(onset=0.1, ts=1.0)
        assert new_state == _PENDING_SILENCE

    def test_pending_silence_back_to_speech(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SILENCE,
            _SPEECH,
            _VADFSM,
        )

        fsm = _VADFSM()
        fsm.state = _PENDING_SILENCE
        fsm.pending_start_ts = 0.0
        new_state, _ = fsm.step(onset=0.7, ts=0.2)
        assert new_state == _SPEECH

    def test_pending_silence_to_silence_after_min_silence(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            _PENDING_SILENCE,
            _SILENCE,
            _VADFSM,
        )

        fsm = _VADFSM(min_silence_sec=0.05)
        fsm.state = _PENDING_SILENCE
        fsm.pending_start_ts = 0.0
        new_state, transition = fsm.step(onset=0.1, ts=0.1)
        assert new_state == _SILENCE
        assert transition == "segment_end"


# ============================================================
# T3 — MockStreamingASRAdapter behaviour
# ============================================================


class TestMockStreamingASR:
    """Verify MockStreamingASRAdapter delta emission patterns."""

    @pytest.mark.asyncio
    async def test_connect_required_before_push(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRServerError
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(push_latency_ms=0)
        with pytest.raises(StreamingASRServerError):
            await asr.push_pcm(b"\x00" * 1024, seq=1)

    @pytest.mark.asyncio
    async def test_realtime_emitted_on_interval(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0, realtime_interval=4,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        realtime_count = 0
        for i in range(1, 5):  # 4 pushes → 1 realtime at push 4
            delta = await asr.push_pcm(_make_pcm(seed=i), seq=i)
            if delta.mode == "realtime" and delta.text:
                realtime_count += 1
        assert realtime_count == 1

    @pytest.mark.asyncio
    async def test_confirmed_emitted_on_interval(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=2, confirmed_interval=4,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        confirmed_count = 0
        for i in range(1, 5):
            delta = await asr.push_pcm(_make_pcm(seed=i), seq=i)
            if delta.mode == "confirmed" and delta.is_final:
                confirmed_count += 1
        assert confirmed_count == 1

    @pytest.mark.asyncio
    async def test_finalize_returns_confirmed_delta(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        deltas = await asr.finalize()
        assert len(deltas) == 1
        assert deltas[0].mode == "confirmed"
        assert deltas[0].is_final

    @pytest.mark.asyncio
    async def test_finalize_after_close_returns_empty(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
        await asr.connect(session_id="s1", tenant_id="t1")
        await asr.aclose()
        deltas = await asr.finalize()
        assert deltas == ()

    @pytest.mark.asyncio
    async def test_deterministic_text_same_input(self) -> None:
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr1 = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=1, confirmed_interval=1,
        )
        asr2 = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=1, confirmed_interval=1,
        )
        await asr1.connect(session_id="s1", tenant_id="t1")
        await asr2.connect(session_id="s2", tenant_id="t2")
        d1 = await asr1.push_pcm(_make_pcm(seed=7), seq=1)
        d2 = await asr2.push_pcm(_make_pcm(seed=7), seq=1)
        assert d1.text == d2.text

    @pytest.mark.asyncio
    async def test_flaky_mode_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRServerError
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter

        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0, flaky=True,
        )
        await asr.connect(session_id="s1", tenant_id="t1")
        # Push 99 chunks; 100th raises.
        for _i in range(1, 100):
            with contextlib.suppress(Exception):
                # May hit other intervals; ignore until 100th
                await asr.push_pcm(_make_pcm(seed=_i), seq=_i)
        with pytest.raises(StreamingASRServerError):
            await asr.push_pcm(_make_pcm(seed=100), seq=100)


# ============================================================
# T3 — StreamingFunASRAdapter with mock WebSocket client
# ============================================================


class _MockWebSocketClient:
    """Minimal mock WebSocket client mimicking the websockets API."""

    def __init__(
        self,
        *,
        recv_queue: list[str | bytes] | None = None,
    ) -> None:
        self.sent: list[str | bytes] = []
        self._recv_queue = list(recv_queue or [])
        self._closed = False

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise RuntimeError("ws closed")
        self.sent.append(data)

    async def recv(self) -> str | bytes:
        if not self._recv_queue:
            await asyncio.sleep(0.01)
            raise TimeoutError()
        return self._recv_queue.pop(0)

    async def close(self) -> None:
        self._closed = True


class TestStreamingFunASRAdapter:
    """Real ASR adapter with injected mock WebSocket."""

    @pytest.mark.asyncio
    async def test_connect_sends_init_json(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient()
        adapter = StreamingFunASRAdapter(
            ws_url="ws://funasr:10095",
            ws_client=ws,
        )
        await adapter.connect(
            session_id="s1", tenant_id="t1", hotwords=("CS75", "UNI-V"),
        )
        assert len(ws.sent) == 1
        init = json.loads(ws.sent[0])
        assert init["mode"] == "2pass"
        assert init["chunk_size"] == [5, 10, 5]
        assert init["is_speaking"] is True
        assert init["audio_fs"] == 16000
        # Hotwords serialised as JSON blob.
        hotwords = json.loads(init["hotwords"])
        assert hotwords == {"CS75": 1, "UNI-V": 1}

    @pytest.mark.asyncio
    async def test_push_pcm_returns_realtime_delta(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(
            recv_queue=[json.dumps({"mode": "2pass-online", "text": "hello", "is_final": False})],
        )
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s", tenant_id="t")
        delta = await adapter.push_pcm(b"\x00" * 8, seq=1)
        assert delta.mode == "realtime"
        assert delta.text == "hello"
        assert delta.is_final is False

    @pytest.mark.asyncio
    async def test_push_pcm_returns_confirmed_delta(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(
            recv_queue=[json.dumps({"mode": "2pass-offline", "text": "final.", "is_final": True})],
        )
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s", tenant_id="t")
        delta = await adapter.push_pcm(b"\x00" * 8, seq=2)
        assert delta.mode == "confirmed"
        assert delta.is_final is True

    @pytest.mark.asyncio
    async def test_push_timeout_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRPushTimeout
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(recv_queue=[])  # never responds
        adapter = StreamingFunASRAdapter(
            ws_url="ws://x", ws_client=ws, push_timeout_sec=0.05,
        )
        await adapter.connect(session_id="s", tenant_id="t")
        with pytest.raises(StreamingASRPushTimeout):
            await adapter.push_pcm(b"\x00" * 8, seq=1)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_protocol_error(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRProtocolError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(recv_queue=["not json"])
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s", tenant_id="t")
        with pytest.raises(StreamingASRProtocolError):
            await adapter.push_pcm(b"\x00" * 8, seq=1)

    @pytest.mark.asyncio
    async def test_push_before_connect_raises(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRServerError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        adapter = StreamingFunASRAdapter(ws_url="ws://x")  # no ws_client, no connect
        with pytest.raises(StreamingASRServerError):
            await adapter.push_pcm(b"\x00" * 8, seq=1)

    @pytest.mark.asyncio
    async def test_finalize_drains_trailing(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(
            recv_queue=[
                json.dumps({"mode": "2pass-offline", "text": "first.", "is_final": True}),
                json.dumps({"mode": "2pass-offline", "text": "second.", "is_final": True}),
            ],
        )
        adapter = StreamingFunASRAdapter(
            ws_url="ws://x", ws_client=ws, finalize_timeout_sec=1.0,
        )
        await adapter.connect(session_id="s", tenant_id="t")
        deltas = await adapter.finalize()
        assert len(deltas) == 1  # short-circuits at first is_final confirmed
        assert deltas[0].text == "first."

    @pytest.mark.asyncio
    async def test_init_payload_hotwords_serialised(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient()
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(
            session_id="s", tenant_id="t",
            hotwords=("长安CS75", "退订意向", "投诉"),
        )
        init = json.loads(ws.sent[0])
        blob = json.loads(init["hotwords"])
        assert blob == {"长安CS75": 1, "退订意向": 1, "投诉": 1}

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back_to_realtime(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _MockWebSocketClient(
            recv_queue=[json.dumps({"mode": "online", "text": "x", "is_final": False})],
        )
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s", tenant_id="t")
        delta = await adapter.push_pcm(b"\x00", seq=1)
        assert delta.mode == "realtime"


# ============================================================
# T3 — FunASRConnectionPool
# ============================================================


class _PoolingASRAdapter:
    """Tiny stub matching the StreamingASRAdapter interface for pool tests."""

    def __init__(self) -> None:
        self._tenant_id = "default"
        self._closed = False
        self._ws = object()  # truthy → alive
        self.connect_called = False

    async def connect(self, *, session_id: str, tenant_id: str, hotwords: Sequence[str] = ()) -> None:
        self._tenant_id = tenant_id
        self.connect_called = True  # type: ignore[attr-defined]

    async def push_pcm(self, pcm: bytes, *, seq: int):
        return None

    async def finalize(self):
        return ()

    async def aclose(self) -> None:
        self._closed = True
        self._ws = None


class TestFunASRConnectionPool:
    """Verify Q1 per-tenant pool semantics."""

    @pytest.mark.asyncio
    async def test_pool_isolated_per_tenant(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=2)
        assert "t1" not in pool.tenants_known()
        # We can't easily acquire without a real WS — just verify pool lazy-init.
        p1 = await pool._ensure_pool("t1")
        p2 = await pool._ensure_pool("t2")
        assert p1 is not p2
        assert set(pool.tenants_known()) == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_release_returns_to_free_list(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=2)
        # Manually inject a stub into the pool to test release bookkeeping.
        stub = _PoolingASRAdapter()  # type: ignore[abstract]
        stub._tenant_id = "t1"  # match the pool key
        tenant_pool = await pool._ensure_pool("t1")
        tenant_pool.in_use.add(stub)  # type: ignore[arg-type]
        tenant_pool.semaphore._value -= 1  # type: ignore[attr-defined]

        await pool.release(stub)  # type: ignore[arg-type]
        assert stub in tenant_pool.free
        assert stub not in tenant_pool.in_use

    @pytest.mark.asyncio
    async def test_close_all_clears_pools(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")
        await pool._ensure_pool("t1")
        await pool._ensure_pool("t2")
        assert len(pool.tenants_known()) == 2
        await pool.close_all()
        assert pool.tenants_known() == []

    @pytest.mark.asyncio
    async def test_release_discards_dead_adapter(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")
        stub = _PoolingASRAdapter()  # type: ignore[abstract]
        stub._tenant_id = "t1"
        stub._closed = True  # mark dead
        stub._ws = None
        tenant_pool = await pool._ensure_pool("t1")
        tenant_pool.in_use.add(stub)  # type: ignore[arg-type]

        await pool.release(stub)  # type: ignore[arg-type]
        # Dead adapter should NOT be in free list.
        assert stub not in tenant_pool.free

    @pytest.mark.asyncio
    async def test_pool_size_configurable(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=4)
        assert pool.pool_size_per_tenant == 4

    @pytest.mark.asyncio
    async def test_free_count_diagnostics(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")
        assert pool.free_count("nonexistent") == 0
