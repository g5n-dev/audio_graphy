"""M8 Phase 4 — streaming end-to-end integration tests (T12).

Full chain (mock adapters only — no real funASR / Silero services):

    WebSocket client → mock streaming VAD → mock streaming ASR
        → (confirmed events) → tag scheduler → retrieval query
        → metrics counters → session close

Plus:
    - Concurrent 2-session isolation.
    - ``enable_streaming=False`` → /ws/stream 404 (M1-M7 zero regression).
    - Pipeline-level chain: StreamSession → StreamingChunker →
      DeltaGraphUpdater → StreamingRetriever over the same graph + RWLock.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
from audio_graphy.adapters.protocols import StreamSessionId
from audio_graphy.api import metrics as m
from audio_graphy.api.ws_stream import router as ws_router
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.config import Settings
from audio_graphy.core.chunker import ChunkRecord, SegmentRecord
from audio_graphy.core.stream_session import StreamSession
from audio_graphy.core.streaming_chunker import StreamingChunker
from audio_graphy.core.streaming_retrieval import StreamingRetriever
from audio_graphy.core.streaming_rwlock import StreamingRWLock
from audio_graphy.core.streaming_tag_scheduler import StreamingTagScheduler
from audio_graphy.core.types import GraphEdge, GraphNode
from audio_graphy.storage.graph_networkx import NetworkXGraphStore

# ============================================================
# Shared fakes
# ============================================================


@dataclass
class _FakeBatchResult:
    tags_written: int = 0


class _FakeRecomputeService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def recompute_tags_for_segments(
        self,
        tenant_id: str,
        recording_id: int,
        segment_ids: list[int],
    ) -> _FakeBatchResult:
        self.calls.append(
            {"tenant_id": tenant_id, "recording_id": recording_id, "segment_ids": list(segment_ids)}
        )
        return _FakeBatchResult(tags_written=len(segment_ids))


class _KeywordLLM:
    model = "fake-weak"

    def __init__(self, keywords: str = "客户A") -> None:
        self._keywords = keywords

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        **kwargs: Any,
    ) -> Any:
        from audio_graphy.adapters.protocols import LLMResponse

        return LLMResponse(text=self._keywords, model=self.model, prompt_hash="x")


@dataclass
class _FakeBundle:
    weak_llm: _KeywordLLM


# ============================================================
# App factory (mirrors tests/api/test_m8_ws_stream.py)
# ============================================================


def _make_app(
    tmp_path: Path,
    *,
    enable_streaming: bool = True,
    enable_retrieval: bool = True,
    with_tag_scheduler: bool = True,
    tenant_id: str = "t1",
) -> tuple[FastAPI, TestClient, _FakeRecomputeService]:
    settings = Settings(
        jwt_secret="test-secret-32-chars-minimum-length!!",
        enable_streaming=enable_streaming,
        enable_streaming_retrieval=enable_retrieval,
        adapter_streaming_vad_mode="mock",
        adapter_streaming_asr_mode="mock",
        # Generous idle timeout: queued frames are only consumed when the
        # server's receive poll wakes (TestClient portal is serial), so a
        # chunk may sit unprocessed for up to one heartbeat interval.
        streaming_session_timeout_sec=30.0,
        streaming_vad_reset_seq_gap=3,
        # Short poll interval: the TestClient portal runs tasks serially, so
        # the server only notices queued client frames when its receive poll
        # wakes. 30 s would starve every test; 1 s is a workable compromise
        # (extra ``ping`` frames are tolerated by ``_drain_until``).
        ws_heartbeat_interval_sec=1.0,
        streaming_tag_interval=2,
        streaming_tag_debounce_ms=0.0,
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

    fake_svc = _FakeRecomputeService()
    if with_tag_scheduler:

        def _factory(tid: str, rid: int) -> StreamingTagScheduler:
            return StreamingTagScheduler(
                fake_svc,  # type: ignore[arg-type]
                interval_n=settings.streaming_tag_interval,
                debounce_ms=settings.streaming_tag_debounce_ms,
                tenant_id=tid,
                recording_id=rid,
            )

        app.state.tag_scheduler_factory = _factory

    # Streaming retriever over a seeded per-tenant graph.
    store = NetworkXGraphStore(tmp_path, tenant_id=tenant_id)
    rwlock = StreamingRWLock()
    app.state.graph_rw_lock = rwlock
    app.state.streaming_graph_store = store
    app.state.streaming_retriever = StreamingRetriever(
        lambda _t: store,
        rwlock,
        _FakeBundle(weak_llm=_KeywordLLM("客户A")),  # type: ignore[arg-type]
    )

    app.include_router(ws_router)
    client = TestClient(app, raise_server_exceptions=False)
    return app, client, fake_svc


def _open_session(client: TestClient, app: FastAPI, session_id: str, tenant: str = "t1") -> Any:
    """Return an un-entered WS context manager that sends ``init`` on enter.

    Caller MUST use it exactly once as ``with _open_session(...) as ws:``.
    The session handshake (init → session_opened) runs inside ``__enter__``.
    """
    mgr: JWTManager = app.state.jwt_manager
    token = mgr.create_access_token(user_id=1, tenant_id=tenant, role="agent")
    return _InitOnEnter(client.websocket_connect(f"/ws/stream?token={token}"), session_id)


class _InitOnEnter:
    """Wrap a WebSocketTestSession CM: enter it, then perform the init handshake."""

    def __init__(self, ws_cm: Any, session_id: str) -> None:
        self._ws_cm = ws_cm
        self._session_id = session_id

    def __enter__(self) -> Any:
        ws = self._ws_cm.__enter__()
        ws.send_text(
            json.dumps(
                {
                    "type": "init",
                    "session_id": self._session_id,
                    "recording_id": 1,
                    "consent_token": "yes",
                }
            )
        )
        first = json.loads(ws.receive_text())
        assert first["type"] == "session_opened"
        return ws

    def __exit__(self, *exc: Any) -> Any:
        return self._ws_cm.__exit__(*exc)


def _pcm_frame(seq: int, payload: bytes = b"\x01" * 1024) -> bytes:
    return struct.pack(">I", seq) + payload


def _drain_until(ws: Any, wanted: set[str], max_msgs: int = 60) -> list[dict[str, Any]]:
    """Receive events until all ``wanted`` types seen or max_msgs reached."""
    events: list[dict[str, Any]] = []
    for _ in range(max_msgs):
        try:
            payload = json.loads(ws.receive_text())
        except Exception:
            break
        events.append(payload)
        if wanted and wanted <= {e.get("type") for e in events}:
            break
        if payload.get("type") == "session_closed":
            break
    return events


# ============================================================
# Full-chain event flow
# ============================================================


class TestFullChainEventFlow:
    def test_session_opened_to_closed_happy_path(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-1") as ws:
            for seq in range(8):
                ws.send_bytes(_pcm_frame(seq))
            ws.send_text(json.dumps({"type": "finalize"}))
            events = _drain_until(ws, {"session_closed"})
        types = [e.get("type") for e in events]
        assert "session_closed" in types
        assert "error" not in types

    def test_realtime_and_confirmed_events_emitted(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-2") as ws:
            for seq in range(12):  # mock ASR confirms every 12th push
                ws.send_bytes(_pcm_frame(seq, bytes([seq % 256]) * 1024))
            ws.send_text(json.dumps({"type": "finalize"}))
            events = _drain_until(ws, {"session_closed"})
        types = {e.get("type") for e in events}
        assert "realtime_text" in types
        assert "segment_confirmed" in types

    def test_tags_updated_event_after_interval(self, tmp_path: Path) -> None:
        """T9 wiring: tag interval=2 → a tags_updated event must appear.

        Mock ASR confirms every 12th push, so 24 frames yield 2 mid-stream
        confirmed segments — exactly the scheduler interval. Note: confirmed
        segments drained during ``finalize`` do NOT feed the scheduler (they
        are emitted by ``session.on_finalize()`` outside ``_handle_binary``).
        """
        app, client, svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-3") as ws:
            for seq in range(24):  # 2 mid-stream confirmed → scheduler triggers
                ws.send_bytes(_pcm_frame(seq, bytes([seq % 256]) * 1024))
            ws.send_text(json.dumps({"type": "finalize"}))
            events = _drain_until(ws, {"session_closed"})
        tag_events = [e for e in events if e.get("type") == "tags_updated"]
        assert tag_events, f"no tags_updated in {[e.get('type') for e in events]}"
        assert tag_events[0]["recording_id"] == 1
        assert tag_events[0]["segment_count"] == 2
        # Mid-stream trigger must arrive BEFORE session_closed.
        types = [e.get("type") for e in events]
        assert types.index("tags_updated") < types.index("session_closed")
        assert svc.calls, "recompute service never invoked"
        assert svc.calls[0]["segment_ids"] == [11, 23]

    def test_query_returns_retrieval_result(self, tmp_path: Path) -> None:
        """T10 wiring: query control frame → retrieval_result event."""
        app, client, _svc = _make_app(tmp_path)
        store: NetworkXGraphStore = app.state.streaming_graph_store

        async def _seed() -> None:
            await store.upsert_node(
                GraphNode(
                    entity_id="客户A",
                    name="客户A",
                    type="客户",
                    description="",
                    source_ids=["1_1"],
                    recording_ids=[1],
                )
            )

        asyncio.run(_seed())

        with client, _open_session(client, app, "e2e-4") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "query",
                        "query": "客户A 怎么样",
                        "top_k": 5,
                    }
                )
            )
            events = _drain_until(ws, {"retrieval_result"}, max_msgs=5)
        results = [e for e in events if e.get("type") == "retrieval_result"]
        assert results, f"no retrieval_result in {[e.get('type') for e in events]}"
        assert results[0]["result"]["candidates"]

    def test_query_with_min_confidence_strict_mode(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        store: NetworkXGraphStore = app.state.streaming_graph_store

        async def _seed() -> None:
            await store.upsert_node(
                GraphNode(
                    entity_id="客户A",
                    name="客户A",
                    type="客户",
                    description="",
                    source_ids=["1_1"],
                    recording_ids=[1],
                )
            )
            await store.upsert_node(
                GraphNode(
                    entity_id="UNI-V",
                    name="UNI-V",
                    type="车型",
                    description="",
                    source_ids=["1_3"],
                    recording_ids=[1],
                )
            )
            await store.upsert_edge(
                GraphEdge(
                    source="客户A",
                    target="UNI-V",
                    relation="听说",
                    weight=1.0,
                    confidence="AMBIGUOUS",
                    confidence_score=None,
                    source_ids=["1_3"],
                )
            )

        asyncio.run(_seed())

        with client, _open_session(client, app, "e2e-5") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "query",
                        "query": "客户A",
                        "top_k": 10,
                        "min_confidence": "EXTRACTED",
                    }
                )
            )
            events = _drain_until(ws, {"retrieval_result"}, max_msgs=5)
        result = next(e for e in events if e.get("type") == "retrieval_result")
        edge_cands = [c for c in result["result"]["candidates"] if c["depth"] == 1]
        assert all(c["confidence"] == "EXTRACTED" for c in edge_cands)

    def test_query_disabled_returns_error_event(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path, enable_retrieval=False)
        with client, _open_session(client, app, "e2e-6") as ws:
            ws.send_text(json.dumps({"type": "query", "query": "x"}))
            events = _drain_until(ws, {"error"}, max_msgs=5)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors and errors[0]["code"] == "RETRIEVAL_DISABLED"
        assert errors[0]["recoverable"] is True

    def test_query_empty_text_returns_error(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-7") as ws:
            ws.send_text(json.dumps({"type": "query", "query": "  "}))
            events = _drain_until(ws, {"error"}, max_msgs=5)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors and errors[0]["code"] == "BAD_QUERY"

    def test_vad_reset_on_seq_gap(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-8") as ws:
            ws.send_bytes(_pcm_frame(0))
            ws.send_bytes(_pcm_frame(10))  # gap 10 > 3 → reset
            events = _drain_until(ws, {"vad_reset"}, max_msgs=5)
        resets = [e for e in events if e.get("type") == "vad_reset"]
        assert resets and resets[0]["reason"] == "seq_gap"

    def test_finalize_yields_session_closed_with_stats(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        with client, _open_session(client, app, "e2e-9") as ws:
            for seq in range(4):
                ws.send_bytes(_pcm_frame(seq))
            ws.send_text(json.dumps({"type": "finalize"}))
            events = _drain_until(ws, {"session_closed"})
        closed = next(e for e in events if e.get("type") == "session_closed")
        # ``bytes_in`` counts PCM payload only (4-byte seq header excluded).
        assert closed["stats"]["bytes_in"] >= 4 * 1024
        assert closed["reason"] == "normal"


# ============================================================
# Metrics integration
# ============================================================


def _sample(name: str, labels: dict[str, str]) -> float:
    for metric in m.REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


class TestMetricsIntegration:
    def test_sessions_total_incremented(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path, tenant_id="t1")
        before = _sample("audiography_streaming_sessions_total", {"tenant_id": "t1"})
        with client, _open_session(client, app, "e2e-m1") as ws:
            ws.send_text(json.dumps({"type": "finalize"}))
            _drain_until(ws, {"session_closed"})
        after = _sample("audiography_streaming_sessions_total", {"tenant_id": "t1"})
        assert after == before + 1

    def test_sessions_active_returns_to_baseline(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        before = _sample("audiography_streaming_sessions_active", {})
        with client, _open_session(client, app, "e2e-m2") as ws:
            during = _sample("audiography_streaming_sessions_active", {})
            assert during == before + 1
            ws.send_text(json.dumps({"type": "finalize"}))
            _drain_until(ws, {"session_closed"})
        # The gauge dec happens in the endpoint finally block.
        # TestClient closes the socket after context exit; poll briefly.
        import time

        for _ in range(20):
            if _sample("audiography_streaming_sessions_active", {}) == before:
                break
            time.sleep(0.05)
        assert _sample("audiography_streaming_sessions_active", {}) == before

    def test_confirmed_segments_counted(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        before = _sample("audiography_streaming_segments_total", {"mode": "confirmed"})
        with client, _open_session(client, app, "e2e-m3") as ws:
            for seq in range(12):
                ws.send_bytes(_pcm_frame(seq, bytes([seq % 256]) * 1024))
            ws.send_text(json.dumps({"type": "finalize"}))
            _drain_until(ws, {"session_closed"})
        after = _sample("audiography_streaming_segments_total", {"mode": "confirmed"})
        assert after > before

    def test_vad_reset_counted(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        before = _sample("audiography_streaming_vad_resets_total", {"reason": "seq_gap"})
        with client, _open_session(client, app, "e2e-m4") as ws:
            ws.send_bytes(_pcm_frame(0))
            ws.send_bytes(_pcm_frame(9))  # gap > 3
            _drain_until(ws, {"vad_reset"}, max_msgs=5)
        after = _sample("audiography_streaming_vad_resets_total", {"reason": "seq_gap"})
        assert after == before + 1

    def test_tag_recompute_counted(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        before = _sample("audiography_streaming_tag_recomputes_total", {"status": "ok"})
        with client, _open_session(client, app, "e2e-m5") as ws:
            for seq in range(12):
                ws.send_bytes(_pcm_frame(seq, bytes([seq % 256]) * 1024))
            ws.send_text(json.dumps({"type": "finalize"}))
            _drain_until(ws, {"session_closed"})
        after = _sample("audiography_streaming_tag_recomputes_total", {"status": "ok"})
        assert after > before

    def test_asr_latency_histogram_observed(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        before = _sample("audiography_streaming_asr_latency_seconds_count", {})
        with client, _open_session(client, app, "e2e-m6") as ws:
            for seq in range(12):
                ws.send_bytes(_pcm_frame(seq, bytes([seq % 256]) * 1024))
            ws.send_text(json.dumps({"type": "finalize"}))
            _drain_until(ws, {"session_closed"})
        after = _sample("audiography_streaming_asr_latency_seconds_count", {})
        assert after > before


# ============================================================
# Concurrent session isolation
# ============================================================


class TestConcurrentSessionIsolation:
    def test_two_sessions_independent_registries(self, tmp_path: Path) -> None:
        """Two sequential sessions: registry lifecycle + event scoping.

        NOTE: the Starlette TestClient portal executes one coroutine at a
        time, and a session task blocked in ``ws.receive()`` (timeout =
        heartbeat interval) starves every other task — so truly interleaved
        concurrent WebSocket sessions cannot be exercised through TestClient.
        We therefore verify the isolation properties sequentially:

        1. Session A is registered while open and unregistered on close.
        2. Session B gets a fresh registry entry (no residue from A).
        3. Events received on each socket carry only their own session_id.
        """
        app, client, _svc = _make_app(tmp_path)
        with client:
            with _open_session(client, app, "iso-1", tenant="t1") as ws1:
                assert set(app.state.stream_sessions) == {"iso-1"}
                # Send PCM + finalize up-front: the TestClient portal only
                # flushes the client send queue when the client blocks on a
                # receive, so batching avoids ping-storm stalls.
                for seq in range(3):
                    ws1.send_bytes(_pcm_frame(seq))
                ws1.send_text(json.dumps({"type": "finalize"}))
                events1 = _drain_until(ws1, {"session_closed"})
                scoped1 = [e for e in events1 if "session_id" in e]
                assert scoped1 and all(e["session_id"] == "iso-1" for e in scoped1)
                closed1 = next(e for e in events1 if e["type"] == "session_closed")
                assert closed1["reason"] == "normal"
            assert app.state.stream_sessions == {}

            with _open_session(client, app, "iso-2", tenant="t1") as ws2:
                # Fresh registry: only iso-2 present, no iso-1 residue.
                assert set(app.state.stream_sessions) == {"iso-2"}
                for seq in range(3):
                    ws2.send_bytes(_pcm_frame(seq))
                ws2.send_text(json.dumps({"type": "finalize"}))
                events2 = _drain_until(ws2, {"session_closed"})
                scoped2 = [e for e in events2 if "session_id" in e]
                assert scoped2 and all(e["session_id"] == "iso-2" for e in scoped2)
                closed2 = next(e for e in events2 if e["type"] == "session_closed")
                assert closed2["reason"] == "normal"
        assert app.state.stream_sessions == {}

    def test_tenant_scoped_metrics_labels(self, tmp_path: Path) -> None:
        app, client, _svc = _make_app(tmp_path)
        mgr: JWTManager = app.state.jwt_manager
        before_a = _sample("audiography_streaming_sessions_total", {"tenant_id": "t1"})
        before_b = _sample("audiography_streaming_sessions_total", {"tenant_id": "t2"})
        with client:
            for tenant, sid in (("t1", "ten-1"), ("t2", "ten-2")):
                token = mgr.create_access_token(user_id=1, tenant_id=tenant, role="agent")
                with client.websocket_connect(f"/ws/stream?token={token}") as ws:
                    ws.send_text(
                        json.dumps(
                            {
                                "type": "init",
                                "session_id": sid,
                                "recording_id": 1,
                                "consent_token": "yes",
                            }
                        )
                    )
                    json.loads(ws.receive_text())
                    ws.send_text(json.dumps({"type": "finalize"}))
                    _drain_until(ws, {"session_closed"})
        assert _sample("audiography_streaming_sessions_total", {"tenant_id": "t1"}) == before_a + 1
        assert _sample("audiography_streaming_sessions_total", {"tenant_id": "t2"}) == before_b + 1


# ============================================================
# enable_streaming=False regression
# ============================================================


class TestStreamingDisabled:
    def test_ws_route_absent_when_disabled(self, tmp_path: Path) -> None:
        """enable_streaming=False → no /ws/stream route (404 on connect)."""
        settings = Settings(
            jwt_secret="test-secret-32-chars-minimum-length!!",
            enable_streaming=False,
        )
        app = FastAPI()
        app.state.settings = settings
        # Mirror main.py: router only included when enable_streaming=True.
        if settings.enable_streaming:
            app.include_router(ws_router)
        client = TestClient(app, raise_server_exceptions=False)
        with (
            client,
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/stream?token=x"),
        ):
            pass

    def test_main_app_has_no_ws_route_by_default(self) -> None:
        """The production app (default settings) exposes no /ws/stream."""
        from audio_graphy.main import create_app

        app = create_app()
        paths = [getattr(r, "path", "") for r in app.routes]
        assert "/ws/stream" not in paths


# ============================================================
# Pipeline-level chain: session → chunker → graph → retriever
# ============================================================


class TestPipelineChain:
    @pytest.mark.asyncio
    async def test_chunker_feeds_graph_retriever_reads(self, tmp_path: Path) -> None:
        """StreamingChunker output → graph upsert (under write lock) →
        StreamingRetriever (under read lock) — the WS-3 mini-pipeline."""
        store = NetworkXGraphStore(tmp_path, tenant_id="t1")
        rwlock = StreamingRWLock()
        chunker = StreamingChunker(token_budget=50)

        # Feed confirmed segments through the chunker.
        emitted: list[ChunkRecord] = []
        for i, text in enumerate(
            [
                "客户A 询问了 长安CS75 的价格。" * 5,
                "坐席推荐了金融方案。" * 5,
            ]
        ):
            seg = SegmentRecord(
                idx=i,
                start_sec=float(i),
                end_sec=float(i + 1),
                transcript=text,
                speaker=None,
                vad_conf=1.0,
            )
            chunk = chunker.push_segment(seg)
            if chunk is not None:
                emitted.append(chunk)
        tail = chunker.flush()
        if tail is not None:
            emitted.append(tail)
        assert emitted, "chunker emitted nothing"

        # Simulate DeltaGraphUpdater's write path (graph upsert under lock).
        async with rwlock.write_lock():
            await store.upsert_node(
                GraphNode(
                    entity_id="客户A",
                    name="客户A",
                    type="客户",
                    description="",
                    source_ids=[f"1_{emitted[0].segment_ids[0]}"],
                    recording_ids=[1],
                )
            )
            await store.upsert_node(
                GraphNode(
                    entity_id="长安CS75",
                    name="长安CS75",
                    type="车型",
                    description="",
                    source_ids=["1_0"],
                    recording_ids=[1],
                )
            )
            await store.upsert_edge(
                GraphEdge(
                    source="客户A",
                    target="长安CS75",
                    relation="询问",
                    weight=2.0,
                    confidence="EXTRACTED",
                    confidence_score=1.0,
                    source_ids=["1_0"],
                )
            )

        # Retrieval over the just-updated subgraph.
        retriever = StreamingRetriever(
            lambda _t: store,
            rwlock,
            _FakeBundle(weak_llm=_KeywordLLM("客户A")),  # type: ignore[arg-type]
        )
        result = await retriever.retrieve("客户A 问了什么", tenant_id="t1", top_k=5)
        ids = {c.entity_id for c in result.candidates}
        assert "客户A" in ids
        assert "长安CS75" in ids

    @pytest.mark.asyncio
    async def test_session_confirmed_flows_into_chunker(self) -> None:
        """StreamSession confirmed ASR text → SegmentRecord → StreamingChunker.

        The mock VAD only closes a speech segment at chunk #200, so within a
        short stream ``confirmed_segments`` stays empty; confirmed text from
        the ASR side is what actually feeds the WS-3 chunker (the
        DeltaGraphUpdater consumes transcript, not audio). We therefore
        materialise one SegmentRecord per confirmed event — exactly what the
        production wiring does — and push it through the chunker.
        """
        vad = MockStreamingVADAdapter()
        asr = MockStreamingASRAdapter(confirmed_interval=2, realtime_interval=1)
        await asr.connect(session_id="s", tenant_id="t1")
        session = StreamSession(
            session_id=StreamSessionId(value="pipe-1"),
            tenant_id="t1",
            recording_id=1,
            user_id=1,
            consent_token_hash="x",
            vad_adapter=vad,
            asr_adapter=asr,
        )
        session.mark_active()
        events: list[dict[str, Any]] = []
        for seq in range(4):
            async for event in session.on_pcm_chunk(b"\x00" * 1024, seq=seq):
                events.append(event)
        confirmed = [e for e in events if e["type"] == "segment_confirmed"]
        assert confirmed, f"no confirmed events: {[e['type'] for e in events]}"

        # Materialise SegmentRecords from the confirmed ASR text and feed the chunker.
        segments = [
            SegmentRecord(
                idx=i,
                start_sec=float(i),
                end_sec=float(i + 1),
                transcript=e["text"],
                speaker=None,
                vad_conf=1.0,
            )
            for i, e in enumerate(confirmed)
        ]
        chunker = StreamingChunker(token_budget=10)
        chunks = [c for seg in segments if (c := chunker.push_segment(seg)) is not None]
        tail = chunker.flush()
        if tail is not None:
            chunks.append(tail)
        assert chunks, "chunker emitted nothing from confirmed segments"
        assert all(ch.text.strip() for ch in chunks)
