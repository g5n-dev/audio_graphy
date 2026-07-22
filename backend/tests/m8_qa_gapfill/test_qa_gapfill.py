"""M8 QA gap-fill tests (严过关) — targeted coverage for M8 modules < 90%.

No source modifications. Covers:
- DeltaGraphUpdater (constructor, update happy path, hash-skip, edge tagging)
- StreamingSileroVADAdapter (push_chunk lifecycle via fake ONNX, finalize, aclose)
- StreamingFunASRAdapter (connect paths, push happy, finalize edge cases, aclose)
- FunASRConnectionPool (acquire/release/close_all, release-missing-pool)
- api/ws_stream (init-frame validation, control-frame routing, helper units)
- StreamSession (attach-confirmed-text branches, on_pcm_chunk guards)
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
from audio_graphy.adapters.protocols import (
    LLMResponse,
    StreamSessionId,
    VADEvent,
)
from audio_graphy.api.ws_stream import (
    _build_tag_scheduler,
    _persist_session_row,
    _register_session,
    _unregister_session,
    router as ws_router,
)
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.config import Settings
from audio_graphy.core.chunker import ChunkRecord, SegmentRecord
from audio_graphy.core.delta_graph_updater import DeltaGraphUpdater
from audio_graphy.core.stream_session import StreamSession, hash_consent_token
from audio_graphy.core.streaming_rwlock import StreamingRWLock
from audio_graphy.storage.graph_networkx import NetworkXGraphStore

# ============================================================
# Shared helpers
# ============================================================


def _pcm(seed: int = 0, samples: int = 512) -> bytes:
    return bytes(((seed + i) & 0xFF) for i in range(samples * 2))


def _make_session(**overrides: Any) -> StreamSession:
    asr = MockStreamingASRAdapter(connect_latency_ms=0, push_latency_ms=0)
    kwargs: dict[str, Any] = dict(
        session_id=StreamSessionId(value="qa-gap"),
        tenant_id="t1",
        recording_id=1,
        user_id=1,
        consent_token_hash=hash_consent_token("yes"),
        vad_adapter=MockStreamingVADAdapter(latency_ms=0),
        asr_adapter=asr,
    )
    kwargs.update(overrides)
    return StreamSession(**kwargs)


# ============================================================
# Fake ONNX runtime for StreamingSileroVADAdapter
# ============================================================


class _FakeMeta:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeONNXSession:
    """Fake onnxruntime.InferenceSession returning scripted onset scores."""

    def __init__(self, onsets: list[float]) -> None:
        self._onsets = list(onsets)
        self._idx = 0

    def get_inputs(self) -> list[_FakeMeta]:
        return [_FakeMeta("input"), _FakeMeta("h"), _FakeMeta("c")]

    def get_outputs(self) -> list[_FakeMeta]:
        return [_FakeMeta("output"), _FakeMeta("hn"), _FakeMeta("cn")]

    def run(self, _names: Any, feeds: dict[str, Any]) -> list[Any]:
        onset = self._onsets[min(self._idx, len(self._onsets) - 1)]
        self._idx += 1
        return [
            np.array([[onset]], dtype=np.float32),
            feeds["h"],
            feeds["c"],
        ]


class _FakeONNXSessionFailing(_FakeONNXSession):
    def run(self, _names: Any, feeds: dict[str, Any]) -> list[Any]:
        raise RuntimeError("onnx boom")


def _make_vad_with_onnx(onsets: list[float], **kwargs: Any):
    from audio_graphy.adapters.real.streaming_vad_silero import (
        StreamingSileroVADAdapter,
    )

    adapter = StreamingSileroVADAdapter(model_path="/fake/silero.onnx", **kwargs)
    adapter._sess = _FakeONNXSession(onsets)
    return adapter


def _speak_into_speech(adapter: Any) -> None:
    """Drive the FSM into SPEECH state, bypassing wall-clock promotion."""
    import time as _time

    now = _time.monotonic()
    fsm = adapter._fsm
    fsm.step(0.9, now)  # SILENCE → PENDING_SPEECH
    fsm.step(0.9, now + adapter._min_speech_sec + 0.01)  # → SPEECH
    adapter._speech_start_ts = fsm.speech_start_ts


# ============================================================
# StreamingSileroVADAdapter — push/finalize/close paths
# ============================================================


class TestSileroVADPushLifecycle:
    @pytest.mark.asyncio
    async def test_push_chunk_returns_vad_event(self) -> None:
        adapter = _make_vad_with_onnx([0.9] * 30)
        ev = await adapter.push_chunk(_pcm(seed=0), seq=0)
        assert ev.seq == 0
        assert 0.0 <= ev.onset_score <= 1.0
        assert ev.transition in ("chunk", "segment_start", "segment_end")

    @pytest.mark.asyncio
    async def test_segment_start_via_fsm_promotion(self) -> None:
        """Directly verify the FSM segment_start transition + PCM buffering."""
        adapter = _make_vad_with_onnx([0.9] * 30)
        _speak_into_speech(adapter)
        assert adapter._fsm.state == "SPEECH"
        assert adapter._speech_start_ts is not None
        # Next push keeps SPEECH and buffers PCM.
        ev = await adapter.push_chunk(_pcm(seed=1), seq=1)
        assert ev.state in ("SPEECH", "PENDING_SILENCE")
        assert len(adapter._speech_pcm_buf) >= 1024

    @pytest.mark.asyncio
    async def test_segment_end_roundtrip(self) -> None:
        adapter = _make_vad_with_onnx([0.0] * 12)
        _speak_into_speech(adapter)
        # Drive FSM to PENDING_SILENCE then past min_silence.
        import time as _time

        now = _time.monotonic()
        adapter._fsm.step(0.0, now)  # SPEECH → PENDING_SILENCE
        # Next real push with low onset triggers segment_end via wall-clock —
        # but wall-clock now ≈ pending_start_ts, so force promotion directly.
        adapter._fsm.step(
            0.0, now + adapter._min_silence_sec + 0.01,
        )
        assert adapter._fsm.state == "SILENCE"
        # finalize the segment manually (as segment_end path would).
        trailing = await adapter.finalize()
        assert len(trailing) == 1
        assert trailing[0].transcript == ""
        assert trailing[0].vad_conf == 1.0

    @pytest.mark.asyncio
    async def test_finalize_flushes_in_progress_speech(self) -> None:
        adapter = _make_vad_with_onnx([0.9] * 30)
        _speak_into_speech(adapter)
        assert adapter._speech_start_ts is not None
        trailing = await adapter.finalize()
        assert len(trailing) == 1
        assert adapter._speech_start_ts is None
        # Second finalize → empty.
        assert await adapter.finalize() == ()

    @pytest.mark.asyncio
    async def test_finalize_empty_when_no_speech(self) -> None:
        adapter = _make_vad_with_onnx([0.0] * 10)
        for seq in range(5):
            await adapter.push_chunk(_pcm(seed=seq), seq=seq)
        assert await adapter.finalize() == ()

    @pytest.mark.asyncio
    async def test_onnx_failure_resets_state_and_returns_zero(self) -> None:
        from audio_graphy.adapters.real.streaming_vad_silero import (
            StreamingSileroVADAdapter,
        )

        adapter = StreamingSileroVADAdapter(model_path="/fake/x.onnx")
        adapter._sess = _FakeONNXSessionFailing([])
        ev = await adapter.push_chunk(_pcm(), seq=0)
        assert ev.onset_score == 0.0
        assert adapter._fsm.state == "SILENCE"

    @pytest.mark.asyncio
    async def test_aclose_drops_session_and_allows_reuse(self) -> None:
        adapter = _make_vad_with_onnx([0.9] * 20)
        await adapter.push_chunk(_pcm(), seq=0)
        assert adapter._sess is not None
        await adapter.aclose()
        assert adapter._sess is None
        # Idempotent.
        await adapter.aclose()

    def test_find_name_fallbacks(self) -> None:
        adapter = _make_vad_with_onnx([0.5])
        assert adapter._find_input_name(("input", "audio")) == "input"
        assert adapter._find_input_name(("nope", "alsono")) == "input"
        assert adapter._find_output_name(("output",)) == "output"
        assert adapter._find_output_name(("missing",)) == "output"


# ============================================================
# StreamingFunASRAdapter — happy paths + close branches
# ============================================================


class _StubWS:
    """Scriptable funASR WebSocket stub."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.recv_queue: list[Any] = []
        self.closed = False
        self.send_raises: Exception | None = None

    async def send(self, data: Any) -> None:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(data)

    async def recv(self) -> Any:
        if not self.recv_queue:
            raise TimeoutError("no more frames")
        item = self.recv_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class TestFunASRConnectAndPush:
    @pytest.mark.asyncio
    async def test_connect_with_injected_ws_sends_init(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        adapter = StreamingFunASRAdapter(ws_url="ws://funasr:10095", ws_client=ws)
        await adapter.connect(
            session_id="s1", tenant_id="t1", hotwords=("长安CS75", "退订"),
        )
        assert len(ws.sent) == 1
        payload = json.loads(ws.sent[0])
        assert payload["mode"] == "2pass"
        assert payload["chunk_size"] == [5, 10, 5]
        assert payload["wav_name"] == "session_s1"
        hot = json.loads(payload["hotwords"])
        assert hot == {"长安CS75": 1, "退订": 1}
        # ctor tenant default → adopt session tenant.
        assert adapter._tenant_id == "t1"

    @pytest.mark.asyncio
    async def test_connect_send_failure_maps_to_server_error(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRServerError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.send_raises = RuntimeError("ws gone")
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        with pytest.raises(StreamingASRServerError):
            await adapter.connect(session_id="s1", tenant_id="t1")

    @pytest.mark.asyncio
    async def test_push_happy_realtime_and_confirmed(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = [
            json.dumps({"mode": "2pass-online", "text": "我想退", "is_final": False}),
            json.dumps({
                "mode": "2pass-offline", "text": "我想退订。", "is_final": True,
                "sentence_id": 3, "confidence": 0.9,
            }),
        ]
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        d1 = await adapter.push_pcm(_pcm(), seq=0)
        assert d1.mode == "realtime" and not d1.is_final
        d2 = await adapter.push_pcm(_pcm(), seq=1)
        assert d2.mode == "confirmed" and d2.is_final
        assert d2.sentence_id == 3
        assert abs(d2.confidence - 0.9) < 1e-6

    @pytest.mark.asyncio
    async def test_push_send_failure_maps_to_server_error(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRServerError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        ws.send_raises = RuntimeError("boom")
        with pytest.raises(StreamingASRServerError, match="push send failed"):
            await adapter.push_pcm(_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_recv_non_utf8_binary_maps_protocol_error(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRProtocolError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = [b"\xff\xfe invalid utf8"]
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        with pytest.raises(StreamingASRProtocolError):
            await adapter.push_pcm(_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_recv_non_dict_json_maps_protocol_error(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRProtocolError
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = ["[1,2,3]"]
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        with pytest.raises(StreamingASRProtocolError, match="not an object"):
            await adapter.push_pcm(_pcm(), seq=0)

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back_to_realtime(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = [json.dumps({"mode": "mystery", "text": "hi"})]
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        delta = await adapter.push_pcm(_pcm(), seq=0)
        assert delta.mode == "realtime"

    @pytest.mark.asyncio
    async def test_finalize_not_connected_returns_empty(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        adapter = StreamingFunASRAdapter(ws_url="ws://x")
        assert await adapter.finalize() == ()

    @pytest.mark.asyncio
    async def test_finalize_send_failure_returns_empty(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.connect(session_id="s1", tenant_id="t1")
        ws.send_raises = RuntimeError("gone")
        assert await adapter.finalize() == ()

    @pytest.mark.asyncio
    async def test_finalize_skips_malformed_frames(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = [
            b"\xff not utf8",
            "not-json{",
            "[1,2]",
            json.dumps({"mode": "2pass-offline", "text": "done", "is_final": True}),
        ]
        adapter = StreamingFunASRAdapter(
            ws_url="ws://x", ws_client=ws, finalize_timeout_sec=2.0,
        )
        await adapter.connect(session_id="s1", tenant_id="t1")
        deltas = await adapter.finalize()
        assert len(deltas) == 1
        assert deltas[0].mode == "confirmed"

    @pytest.mark.asyncio
    async def test_finalize_drain_exception_breaks(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        ws = _StubWS()
        ws.recv_queue = [RuntimeError("connection reset")]
        adapter = StreamingFunASRAdapter(
            ws_url="ws://x", ws_client=ws, finalize_timeout_sec=2.0,
        )
        await adapter.connect(session_id="s1", tenant_id="t1")
        assert await adapter.finalize() == ()

    @pytest.mark.asyncio
    async def test_aclose_owned_vs_injected(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr import (
            StreamingFunASRAdapter,
        )

        # Injected ws (not owned): close is a no-op besides flag.
        ws = _StubWS()
        adapter = StreamingFunASRAdapter(ws_url="ws://x", ws_client=ws)
        await adapter.aclose()
        assert adapter._closed is True
        assert ws.closed is False  # caller owns lifecycle

        # Owned ws: close is delegated + ws dropped.
        ws2 = _StubWS()
        adapter2 = StreamingFunASRAdapter(ws_url="ws://x")
        adapter2._ws = ws2  # simulate connected
        await adapter2.aclose()
        assert ws2.closed is True
        assert adapter2._ws is None
        # Idempotent.
        await adapter2.aclose()


# ============================================================
# FunASRConnectionPool — acquire/release/close_all
# ============================================================


class TestFunASRPoolLifecycle:
    @pytest.mark.asyncio
    async def test_acquire_creates_and_connects(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from audio_graphy.adapters.real import streaming_funasr_pool as pool_mod

        created: list[Any] = []

        class _FakeAdapter:
            def __init__(self, **kwargs: Any) -> None:
                self._tenant_id = kwargs.get("tenant_id", "default")
                self._closed = False
                self._ws = object()
                self.connected_with: dict[str, Any] = {}
                created.append(self)

            async def connect(self, *, session_id, tenant_id, hotwords):
                self.connected_with = {
                    "session_id": session_id, "tenant_id": tenant_id,
                    "hotwords": tuple(hotwords),
                }

            async def aclose(self) -> None:
                self._closed = True
                self._ws = None

        monkeypatch.setattr(pool_mod, "StreamingFunASRAdapter", _FakeAdapter)
        pool = pool_mod.FunASRConnectionPool(
            ws_url="ws://funasr:10095/", pool_size_per_tenant=2,
        )
        adapter = await pool.acquire("t1", "s1", ("hot",))
        assert adapter.connected_with["hotwords"] == ("hot",)
        assert pool.free_count("t1") == 0
        assert pool.tenants_known() == ["t1"]

        # Release healthy → returns to free list.
        await pool.release(adapter)
        assert pool.free_count("t1") == 1

        # Re-acquire reuses the free adapter (no new connect).
        adapter2 = await pool.acquire("t1", "s2", ())
        assert adapter2 is adapter
        await pool.release(adapter2)

        # close_all closes everything.
        await pool.close_all()
        assert pool.tenants_known() == []
        assert created[0]._closed is True

    @pytest.mark.asyncio
    async def test_release_when_pool_missing_closes_adapter(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x")

        class _Dead:
            _tenant_id = "ghost"
            _closed = False
            _ws = object()
            closed_flag = False

            async def aclose(self) -> None:
                self.closed_flag = True

        stub = _Dead()
        await pool.release(stub)  # type: ignore[arg-type]
        assert stub.closed_flag is True

    @pytest.mark.asyncio
    async def test_acquire_skips_dead_free_adapter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from audio_graphy.adapters.real import streaming_funasr_pool as pool_mod

        class _FakeAdapter:
            def __init__(self, **kwargs: Any) -> None:
                self._tenant_id = kwargs.get("tenant_id", "default")
                self._closed = False
                self._ws = object()

            async def connect(self, *, session_id, tenant_id, hotwords):
                pass

            async def aclose(self) -> None:
                self._closed = True
                self._ws = None

        monkeypatch.setattr(pool_mod, "StreamingFunASRAdapter", _FakeAdapter)
        pool = pool_mod.FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=2)
        tenant_pool = await pool._ensure_pool("t1")

        dead = _FakeAdapter(tenant_id="t1")
        dead._closed = True  # liveness check fails
        tenant_pool.free.append(dead)

        adapter = await pool.acquire("t1", "s1", ())
        assert adapter is not dead
        assert dead._ws is None  # discarded via _safe_aclose

    @pytest.mark.asyncio
    async def test_acquire_pool_exhausted_times_out(self) -> None:
        from audio_graphy.adapters.exceptions import StreamingASRConnectTimeout
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(
            ws_url="ws://x", pool_size_per_tenant=1, max_wait_sec=0.05,
        )
        tenant_pool = await pool._ensure_pool("t1")
        # Drain the only permit.
        await tenant_pool.semaphore.acquire()
        with pytest.raises(StreamingASRConnectTimeout):
            await pool.acquire("t1", "s1", ())

    def test_pool_size_property(self) -> None:
        from audio_graphy.adapters.real.streaming_funasr_pool import (
            FunASRConnectionPool,
        )

        pool = FunASRConnectionPool(ws_url="ws://x", pool_size_per_tenant=3)
        assert pool.pool_size_per_tenant == 3


# ============================================================
# DeltaGraphUpdater — constructor + update paths
# ============================================================


class _FakeStrongLLM:
    """Returns newline-separated CSV-style extraction output."""

    model = "fake-strong"

    async def complete(self, messages: Any, **kwargs: Any) -> LLMResponse:
        text = (
            '("实体","客户A","客户","desc-a")\n'
            '("实体","长安CS75","车型","desc-b")\n'
            '("关系","客户A","询问","长安CS75","desc-r")'
        )
        return LLMResponse(text=text, model=self.model, prompt_hash="h")


class _GleanLLM(_FakeStrongLLM):
    """First call returns entities; subsequent (gleaning) returns 'no'."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return await super().complete(messages, **kwargs)
        return LLMResponse(text="no", model=self.model, prompt_hash="h")


@dataclass
class _FakeBundle:
    strong_llm: Any
    weak_llm: Any = None


class _FakeMerger:
    def __init__(self, remap: dict[str, str] | None = None) -> None:
        self._remap = remap or {}

    async def merge(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(self._remap.get(name, name), etype) for name, etype in pairs]


class _FakeDBSession:
    """Minimal async DB session: no rows found, tracks added objects."""

    def __init__(self, existing_chunk_id: int | None = None) -> None:
        self._existing_chunk_id = existing_chunk_id
        self.added: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> "_FakeDBSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, _stmt: Any) -> Any:
        existing = self._existing_chunk_id

        class _Res:
            def first(self) -> Any:
                return (existing,) if existing is not None else None

        return _Res()

    def add(self, obj: Any) -> None:
        # Simulate auto-increment id on flush.
        if getattr(obj, "id", None) is None:
            obj.id = 101
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1


def _make_updater(
    tmp_path: Path,
    *,
    db: _FakeDBSession,
    llm: Any | None = None,
    merger_remap: dict[str, str] | None = None,
) -> tuple[DeltaGraphUpdater, NetworkXGraphStore]:
    store = NetworkXGraphStore(tmp_path, tenant_id="t1")
    rwlock = StreamingRWLock()
    bundle = _FakeBundle(strong_llm=llm or _GleanLLM())
    updater = DeltaGraphUpdater(
        bundle,  # type: ignore[arg-type]
        lambda: db,  # type: ignore[arg-type]
        prompt_template="{input_text}",
        merger_factory=lambda _s, _t: _FakeMerger(merger_remap),  # type: ignore[arg-type]
        linker_factory=lambda *a, **k: None,  # type: ignore[arg-type]
        file_index=None,
        graph_store_factory=lambda _t: store,
        rwlock=rwlock,
        session_id="qa-sess",
    )
    return updater, store


class TestDeltaGraphUpdaterPaths:
    @pytest.mark.asyncio
    async def test_update_happy_path_inserts_graph(self, tmp_path: Path) -> None:
        db = _FakeDBSession()
        updater, store = _make_updater(tmp_path, db=db)
        chunk = ChunkRecord(
            segment_ids=[0], text="客户A 询问了 长安CS75 的价格",
            token_n=10, content_hash="qa-hash-1",
        )
        report = await updater.update(chunk, recording_id=1, tenant_id="t1")
        assert report.skipped_by_hash is False
        assert report.chunk_id == 101
        assert report.new_edges == 1
        assert db.commits == 1
        nodes = {n.entity_id for n in await store.get_all_nodes()}
        assert "客户A" in nodes

    @pytest.mark.asyncio
    async def test_update_skips_on_hash_hit(self, tmp_path: Path) -> None:
        db = _FakeDBSession(existing_chunk_id=55)
        updater, store = _make_updater(tmp_path, db=db)
        chunk = ChunkRecord(
            segment_ids=[0], text="重复文本", token_n=2, content_hash="dup",
        )
        report = await updater.update(chunk, recording_id=1, tenant_id="t1")
        assert report.skipped_by_hash is True
        assert report.chunk_id == 55
        assert report.new_edges == 0
        assert db.added == []  # nothing persisted

    @pytest.mark.asyncio
    async def test_remapped_endpoint_marks_edge_ambiguous(
        self, tmp_path: Path,
    ) -> None:
        db = _FakeDBSession()
        updater, _store = _make_updater(
            tmp_path, db=db, merger_remap={"客户A": "客户A- canonical"},
        )
        chunk = ChunkRecord(
            segment_ids=[0], text="客户A 询问 长安CS75", token_n=5,
            content_hash="qa-hash-2",
        )
        report = await updater.update(chunk, recording_id=1, tenant_id="t1")
        assert report.ambiguous_edges == 1
        assert report.new_edges == 1

    def test_extract_merge_scores_returns_empty(self, tmp_path: Path) -> None:
        db = _FakeDBSession()
        updater, _ = _make_updater(tmp_path, db=db)
        assert updater._extract_merge_scores(object()) == []

    def test_count_entity_outcomes_empty_scores(self, tmp_path: Path) -> None:
        db = _FakeDBSession()
        updater, _ = _make_updater(tmp_path, db=db)
        assert updater._count_entity_outcomes([]) == (0, 0)


# ============================================================
# StreamSession — remaining branches
# ============================================================


class TestStreamSessionRemainingBranches:
    @pytest.mark.asyncio
    async def test_on_pcm_chunk_ignored_after_close(self) -> None:
        session = _make_session()
        await session.asr_adapter.connect(session_id="s", tenant_id="t1")
        session.mark_end(reason="manual")
        events = [e async for e in session.on_pcm_chunk(_pcm(), seq=0)]
        assert events == []

    def test_mark_active_idempotent_after_active(self) -> None:
        session = _make_session()
        session.mark_active()
        session.mark_active()  # second call no-op
        from audio_graphy.core.stream_session import SessionStatus

        assert session.status == SessionStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_attach_confirmed_text_appends_to_existing(self) -> None:
        session = _make_session()
        session.confirmed_segments.append(
            SegmentRecord(idx=0, start_sec=0.0, end_sec=1.0,
                          transcript="hello", speaker=None, vad_conf=1.0)
        )
        session._attach_confirmed_text("world")
        assert session.confirmed_segments[-1].transcript == "hello world"

    def test_attach_confirmed_text_noop_when_empty(self) -> None:
        session = _make_session()
        session._attach_confirmed_text("x")
        assert session.confirmed_segments == []

    def test_attach_confirmed_text_noop_for_non_segmentrecord(self) -> None:
        session = _make_session()
        session.confirmed_segments.append("not-a-segment")
        session._attach_confirmed_text("x")
        assert session.confirmed_segments[-1] == "not-a-segment"

    @pytest.mark.asyncio
    async def test_confirmed_cap_drops_oldest(self) -> None:
        session = _make_session(confirmed_flush_threshold=2)
        for i in range(6):
            session.confirmed_segments.append(
                SegmentRecord(idx=i, start_sec=0.0, end_sec=1.0,
                              transcript=f"t{i}", speaker=None, vad_conf=1.0)
            )
            session._enforce_confirmed_cap()
        # Cap = 2×threshold → keep most recent 4.
        assert len(session.confirmed_segments) == 4
        assert session.confirmed_segments[0].idx == 2

    @pytest.mark.asyncio
    async def test_vad_segment_close_appends_confirmed(self) -> None:
        """VADEvent.segment is buffered into confirmed_segments."""

        class _ClosingVAD:
            def __init__(self) -> None:
                self.calls = 0

            async def push_chunk(self, pcm: bytes, *, seq: int) -> VADEvent:
                self.calls += 1
                if self.calls == 1:
                    return VADEvent(
                        seq=seq, timestamp_sec=0.0, onset_score=0.9,
                        state="SILENCE", transition="segment_end",
                        segment=SegmentRecord(
                            idx=0, start_sec=0.0, end_sec=1.0,
                            transcript="", speaker=None, vad_conf=1.0,
                        ),
                    )
                return VADEvent(
                    seq=seq, timestamp_sec=0.0, onset_score=0.0,
                    state="SILENCE", transition="chunk",
                )

            def reset_state(self) -> None:
                pass

            async def finalize(self) -> tuple:
                return ()

            async def aclose(self) -> None:
                pass

        asr = MockStreamingASRAdapter(
            connect_latency_ms=0, push_latency_ms=0,
            realtime_interval=1, confirmed_interval=2,
        )
        await asr.connect(session_id="s", tenant_id="t1")
        session = _make_session(vad_adapter=_ClosingVAD(), asr_adapter=asr)
        events: list[dict[str, Any]] = []
        for seq in range(2):
            async for ev in session.on_pcm_chunk(_pcm(), seq=seq):
                events.append(ev)
        assert any(e["type"] == "segment_confirmed" for e in events)
        # Confirmed text was attached to the VAD-closed segment.
        assert session.confirmed_segments[-1].transcript


# ============================================================
# api/ws_stream — init validation + control routing + helpers
# ============================================================


def _ws_app(tmp_path: Path, **settings_over: Any) -> tuple[FastAPI, TestClient]:
    settings = Settings(
        jwt_secret="test-secret-32-chars-minimum-length!!",
        enable_streaming=True,
        adapter_streaming_vad_mode="mock",
        adapter_streaming_asr_mode="mock",
        streaming_session_timeout_sec=30.0,
        ws_heartbeat_interval_sec=1.0,
        **settings_over,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.jwt_manager = JWTManager(
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        exp_hours=settings.jwt_exp_hours,
    )
    app.state.session_factory = None
    app.state.streaming_pool = None
    app.state.stream_sessions = {}
    app.include_router(ws_router)
    return app, TestClient(app, raise_server_exceptions=False)


def _token(app: FastAPI, tenant: str = "t1") -> str:
    return app.state.jwt_manager.create_access_token(
        user_id=1, tenant_id=tenant, role="agent",
    )


class TestWSInitValidation:
    def test_first_frame_not_json(self, tmp_path: Path) -> None:
        from starlette.websockets import WebSocketDisconnect

        app, client = _ws_app(tmp_path)
        with client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/ws/stream?token={_token(app)}") as ws:
                    ws.send_text("not json at all")
                    ws.receive_text()
        assert exc_info.value.code == 4001

    def test_first_frame_wrong_type(self, tmp_path: Path) -> None:
        from starlette.websockets import WebSocketDisconnect

        app, client = _ws_app(tmp_path)
        with client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/ws/stream?token={_token(app)}") as ws:
                    ws.send_text(json.dumps({"type": "finalize"}))
                    ws.receive_text()
        assert exc_info.value.code == 4001

    def test_missing_session_id(self, tmp_path: Path) -> None:
        from starlette.websockets import WebSocketDisconnect

        app, client = _ws_app(tmp_path)
        with client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/ws/stream?token={_token(app)}") as ws:
                    ws.send_text(json.dumps({
                        "type": "init", "recording_id": 1, "consent_token": "x",
                    }))
                    ws.receive_text()
        assert exc_info.value.code == 4001

    def test_invalid_recording_id(self, tmp_path: Path) -> None:
        from starlette.websockets import WebSocketDisconnect

        app, client = _ws_app(tmp_path)
        with client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/ws/stream?token={_token(app)}") as ws:
                    ws.send_text(json.dumps({
                        "type": "init", "session_id": "s1",
                        "recording_id": -5, "consent_token": "x",
                    }))
                    ws.receive_text()
        assert exc_info.value.code == 4001

    def test_missing_consent_closes_4002(self, tmp_path: Path) -> None:
        from starlette.websockets import WebSocketDisconnect

        app, client = _ws_app(tmp_path)
        with client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/ws/stream?token={_token(app)}") as ws:
                    ws.send_text(json.dumps({
                        "type": "init", "session_id": "s1", "recording_id": 1,
                    }))
                    ws.receive_text()
        assert exc_info.value.code == 4002


def _open(client: TestClient, app: FastAPI, session_id: str) -> Any:
    class _Ctx:
        def __enter__(self) -> Any:
            self._cm = client.websocket_connect(f"/ws/stream?token={_token(app)}")
            self._ws = self._cm.__enter__()
            self._ws.send_text(json.dumps({
                "type": "init", "session_id": session_id,
                "recording_id": 1, "consent_token": "yes",
            }))
            first = json.loads(self._ws.receive_text())
            assert first["type"] == "session_opened"
            return self._ws

        def __exit__(self, *exc: Any) -> Any:
            return self._cm.__exit__(*exc)

    return _Ctx()


class TestWSControlRouting:
    def test_binary_frame_too_short_yields_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-short") as ws:
            ws.send_bytes(b"\x00\x01")  # < 4 bytes
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "error"
            assert ev["code"] == "BAD_FRAME"

    def test_unknown_control_type_yields_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-unk") as ws:
            ws.send_text(json.dumps({"type": "frobnicate"}))
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "error"
            assert ev["code"] == "UNKNOWN_TYPE"

    def test_bad_json_control_yields_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-badjson") as ws:
            ws.send_text("{not json")
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "error"
            assert ev["code"] == "BAD_JSON"

    def test_non_dict_control_yields_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-nondict") as ws:
            ws.send_text(json.dumps([1, 2, 3]))
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "error"
            assert ev["code"] == "BAD_JSON"

    def test_reset_control_yields_vad_reset(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-reset") as ws:
            ws.send_text(json.dumps({"type": "reset"}))
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "vad_reset"
            assert ev["reason"] == "client_request"

    def test_pong_is_noop(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path)
        with client, _open(client, app, "qa-pong") as ws:
            ws.send_text(json.dumps({"type": "pong"}))
            # No event for pong; next real frame still works.
            ws.send_text(json.dumps({"type": "reset"}))
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "vad_reset"

    def test_query_disabled_returns_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path, enable_streaming_retrieval=False)
        with client, _open(client, app, "qa-qdis") as ws:
            ws.send_text(json.dumps({"type": "query", "query": "hello"}))
            ev = json.loads(ws.receive_text())
            assert ev["type"] == "error"
            assert ev["code"] == "RETRIEVAL_DISABLED"

    def test_query_empty_text_returns_error(self, tmp_path: Path) -> None:
        app, client = _ws_app(tmp_path, enable_streaming_retrieval=True)
        app.state.streaming_retriever = None
        with client, _open(client, app, "qa-qempty") as ws:
            ws.send_text(json.dumps({"type": "query", "query": "   "}))
            ev = json.loads(ws.receive_text())
            # No retriever → RETRIEVER_UNAVAILABLE fires before text check.
            assert ev["type"] == "error"
            assert ev["code"] in ("RETRIEVER_UNAVAILABLE", "BAD_QUERY")


class TestWSHelpers:
    def test_register_unregister_session(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)
        session = _make_session()
        _register_session(app, session)
        assert app.state.stream_sessions["qa-gap"] is session
        _unregister_session(app, session)
        assert "qa-gap" not in app.state.stream_sessions
        # Unregister when registry missing — no-op.
        app.state.stream_sessions = None
        _unregister_session(app, session)

    def test_register_creates_registry_when_missing(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)
        app.state.stream_sessions = None
        session = _make_session()
        _register_session(app, session)
        assert app.state.stream_sessions["qa-gap"] is session

    def test_build_tag_scheduler_no_factory_no_service(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)
        app.state.recompute_service = None

        class _User:
            tenant_id = "t1"

        assert _build_tag_scheduler(app, app.state.settings, _User(), 1) is None

    def test_build_tag_scheduler_factory_exception(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)

        def _boom(_tid: str, _rid: int) -> Any:
            raise RuntimeError("factory boom")

        app.state.tag_scheduler_factory = _boom

        class _User:
            tenant_id = "t1"

        assert _build_tag_scheduler(app, app.state.settings, _User(), 1) is None

    def test_build_tag_scheduler_factory_wrong_type(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)
        app.state.tag_scheduler_factory = lambda _t, _r: "not-a-scheduler"

        class _User:
            tenant_id = "t1"

        assert _build_tag_scheduler(app, app.state.settings, _User(), 1) is None

    def test_build_tag_scheduler_from_recompute_service(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)

        class _Svc:
            async def recompute_tags_for_segments(self, *a: Any, **k: Any) -> Any:
                return None

        app.state.recompute_service = _Svc()

        class _User:
            tenant_id = "t1"

        scheduler = _build_tag_scheduler(app, app.state.settings, _User(), 7)
        assert scheduler is not None

    @pytest.mark.asyncio
    async def test_persist_session_row_no_factory_noop(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)
        app.state.session_factory = None
        session = _make_session()
        await _persist_session_row(app, session)  # must not raise

    @pytest.mark.asyncio
    async def test_persist_session_row_failure_swallowed(self, tmp_path: Path) -> None:
        app, _client = _ws_app(tmp_path)

        class _FailingFactory:
            async def __aenter__(self) -> Any:
                raise RuntimeError("db down")

            async def __aexit__(self, *exc: Any) -> None:
                return None

        app.state.session_factory = lambda: _FailingFactory()
        session = _make_session()
        await _persist_session_row(app, session)  # logged + swallowed
