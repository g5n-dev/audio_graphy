"""WebSocket /ws/stream endpoint — M8 Phase 4 P0-4.

Protocol (architecture §6 + Appendix A):

    Client → Server:
        - First frame (JSON): ``{"type": "init", "session_id": "...",
          "recording_id": 12345, "consent_token": "..."}``.
        - Binary frames: PCM bytes (1024 bytes = 512 samples int16 @ 16kHz).
          Sequence number carried in the WebSocket frame's ``seq`` is NOT
          supported — clients MUST prepend a 4-byte big-endian seq header.
          Binary frame layout: ``[4 bytes seq BE] [N bytes PCM]``.
        - Control JSON: ``{"type": "finalize"}`` / ``{"type": "reset"}``.

    Server → Client:
        - ``session_opened`` — sent after init frame accepted.
        - ``realtime_text`` — one per realtime ASR delta.
        - ``segment_confirmed`` — one per confirmed ASR delta.
        - ``tags_updated`` — emitted after each streaming tag batch (T9).
        - ``retrieval_result`` — answer to a ``query`` control frame (T10).
        - ``backpressure`` — queue depth warning.
        - ``vad_reset`` — emitted when seq-gap triggers reset.
        - ``error`` — non-fatal error (recoverable=true/false).
        - ``session_closed`` — final frame.

    Client control frames (WS-3 additions):
        - ``{"type": "query", "query": "...", "top_k": 5,
          "min_confidence": "EXTRACTED"}`` → one ``retrieval_result`` event.
          Only available when ``settings.enable_streaming_retrieval=True``.

Auth: JWT via ``?token=`` query param (TTL 5 minutes; refresh via REST).
Tenant: derived from JWT claims (``tid``).
Consent: ``consent_token`` required in init frame (PRD §5.3 R10).

Backpressure:
    - ``ws_backpressure_warn`` (default 100) → emit ``backpressure`` event.
    - ``ws_max_recv_queue`` (default 200) → close with code 1011.

Registration (PRD §17.11):
    Only mounted when ``settings.enable_streaming=True``. Default False →
    /ws/stream returns 404 (zero regression for M1-M7).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from audio_graphy.adapters.protocols import StreamSessionId
from audio_graphy.api.metrics import (
    STREAMING_ASR_LATENCY,
    STREAMING_SEGMENTS_TOTAL,
    STREAMING_SESSIONS_ACTIVE,
    STREAMING_SESSIONS_TOTAL,
    STREAMING_TAG_RECOMPUTES_TOTAL,
    STREAMING_VAD_RESETS_TOTAL,
)
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.ws_auth import WS_AUTH_FAILED_CODE, WSAuthUser, verify_ws_token
from audio_graphy.core.stream_session import (
    DEFAULT_CONFIRMED_FLUSH_THRESHOLD,
    DEFAULT_REALTIME_WINDOW,
    SessionStatus,
    StreamSession,
    hash_consent_token,
)

if TYPE_CHECKING:
    from audio_graphy.config import Settings
    from audio_graphy.core.streaming_retrieval import StreamingRetriever
    from audio_graphy.core.streaming_tag_scheduler import StreamingTagScheduler

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

# WS close codes (architecture §6.1.3 + §6.1.4).
WS_CLOSE_NORMAL: int = 1000
WS_CLOSE_BACKPRESSURE: int = 1011
WS_CLOSE_CONSENT_MISSING: int = 4002
WS_CLOSE_TIMEOUT: int = 4003


@router.websocket("/ws/stream")
async def ws_stream(
    ws: WebSocket,
    token: str = Query(..., description="JWT access token (TTL 5min)"),
) -> None:
    """Main streaming endpoint — PCM in, server events out.

    See module docstring for protocol details.
    """
    # --- 1. Resolve settings + JWT manager from app state ---
    settings: Settings = ws.app.state.settings
    jwt_manager: JWTManager = ws.app.state.jwt_manager

    # --- 2. Auth via query string ---
    try:
        user = await verify_ws_token(token, jwt_manager)
    except Exception:
        # verify_ws_token raises WebSocketException which FastAPI closes with.
        raise

    # --- 3. Accept the WS after auth (so 4001 closes cleanly) ---
    await ws.accept()
    logger.info(
        "WS /ws/stream accepted user=%s tenant=%s",
        user.user_id, user.tenant_id,
    )

    # --- 4. Wait for init frame ---
    try:
        init_raw = await asyncio.wait_for(
            ws.receive_text(),
            timeout=settings.streaming_session_timeout_sec,
        )
    except TimeoutError:
        await ws.close(code=WS_CLOSE_TIMEOUT, reason="init timeout")
        return
    except WebSocketDisconnect:
        return

    try:
        init_payload = json.loads(init_raw)
    except json.JSONDecodeError:
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="init not JSON")
        return

    if not isinstance(init_payload, dict) or init_payload.get("type") != "init":
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="first frame must be init")
        return

    session_id_value = str(init_payload.get("session_id", "")).strip()
    if not session_id_value:
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="missing session_id")
        return

    recording_id_raw = init_payload.get("recording_id")
    if not isinstance(recording_id_raw, int) or recording_id_raw <= 0:
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="missing/invalid recording_id")
        return

    consent_token = str(init_payload.get("consent_token", "")).strip()
    if not consent_token:
        # PRD §5.3 R10 — missing consent closes the connection + audit.
        logger.warning(
            "WS /ws/stream consent missing session=%s tenant=%s",
            session_id_value, user.tenant_id,
        )
        await ws.close(code=WS_CLOSE_CONSENT_MISSING, reason="consent_token required")
        return
    consent_hash = hash_consent_token(consent_token)

    # --- 5. Build streaming session (adapters from bundle factory) ---
    from audio_graphy.adapters.bundle import (
        acquire_streaming_adapters_for_session,
    )

    # Hotwords from tenant entity_aliases (M8: leave empty; WS-3 will load).
    hotwords: tuple[str, ...] = ()
    app_pool = getattr(ws.app.state, "streaming_pool", None)

    streaming_bundle = await acquire_streaming_adapters_for_session(
        settings,
        tenant_id=user.tenant_id,
        session_id=session_id_value,
        hotwords=hotwords,
        pool=app_pool,
    )
    # Mock mode: the ASR adapter must be connect()ed before push_pcm works.
    # Real mode: FunASRConnectionPool.acquire() already connects.
    if settings.adapter_streaming_asr_mode != "real":
        await streaming_bundle.asr.connect(
            session_id=session_id_value,
            tenant_id=user.tenant_id,
            hotwords=hotwords,
        )
    session = StreamSession(
        session_id=StreamSessionId(value=session_id_value),
        tenant_id=user.tenant_id,
        recording_id=recording_id_raw,
        user_id=user.user_id,
        consent_token_hash=consent_hash,
        vad_adapter=streaming_bundle.vad,
        asr_adapter=streaming_bundle.asr,
        seq_gap_threshold=settings.streaming_vad_reset_seq_gap,
        pcm_buffer_max_sec=settings.streaming_session_pcm_buffer_max_sec,
        realtime_window=DEFAULT_REALTIME_WINDOW,
        confirmed_flush_threshold=DEFAULT_CONFIRMED_FLUSH_THRESHOLD,
    )
    _register_session(ws.app, session)
    session.mark_active()

    # --- 5b. WS-3 per-session helpers: tag scheduler + retriever (optional DI) ---
    tag_scheduler = _build_tag_scheduler(ws.app, settings, user, recording_id_raw)
    retriever = getattr(ws.app.state, "streaming_retriever", None)

    # T11 metrics — session opened.
    STREAMING_SESSIONS_ACTIVE.inc()
    STREAMING_SESSIONS_TOTAL.labels(tenant_id=user.tenant_id).inc()

    # --- 6. Emit session_opened ---
    await ws.send_text(
        json.dumps(
            {
                "type": "session_opened",
                "session_id": session_id_value,
                "server_time": session.started_at.isoformat(),
                "capabilities": {
                    "max_buffer_chunks": settings.ws_max_recv_queue,
                    "vad_reset_strategy": f"seq_gap_{settings.streaming_vad_reset_seq_gap}",
                },
            },
            ensure_ascii=False,
        )
    )

    # --- 7. Receive / heartbeat loop ---
    try:
        await _run_recv_loop(ws, session, settings, user, tag_scheduler, retriever)
    except WebSocketDisconnect:
        logger.info("WS /ws/stream client disconnect session=%s", session_id_value)
        session.mark_end(reason="client_disconnect")
    except Exception as exc:
        logger.exception("WS /ws/stream error session=%s: %s", session_id_value, exc)
        session.mark_end(reason="error")
        session.error_count += 1
        with contextlib.suppress(Exception):
            await ws.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "session_id": session_id_value,
                        "code": "INTERNAL_ERROR",
                        "message": str(exc)[:200],
                        "recoverable": False,
                    }
                )
            )
    finally:
        # --- 8. Flush pending tag batch + emit session_closed + persist ---
        if tag_scheduler is not None:
            with contextlib.suppress(Exception):
                flushed = await tag_scheduler.flush()
                if flushed is not None:
                    STREAMING_TAG_RECOMPUTES_TOTAL.labels(
                        status="error" if flushed.error else "ok"
                    ).inc()
        STREAMING_SESSIONS_ACTIVE.dec()
        with contextlib.suppress(Exception):
            await _persist_session_row(ws.app, session)
            await ws.send_text(
                json.dumps(
                    {
                        "type": "session_closed",
                        "session_id": session_id_value,
                        "reason": session.end_reason or "normal",
                        "stats": session.stats(),
                    },
                    ensure_ascii=False,
                )
            )
        _unregister_session(ws.app, session)
        with contextlib.suppress(Exception):
            await ws.close(code=WS_CLOSE_NORMAL)


# ------------------------------------------------------------------
# Receive loop
# ------------------------------------------------------------------
async def _run_recv_loop(
    ws: WebSocket,
    session: StreamSession,
    settings: Settings,
    user: WSAuthUser,
    tag_scheduler: StreamingTagScheduler | None = None,
    retriever: StreamingRetriever | None = None,
) -> None:
    """Main recv loop — handle binary + control frames + heartbeat."""
    last_pong = time.monotonic()
    heartbeat_interval = settings.ws_heartbeat_interval_sec
    warn_threshold = settings.ws_backpressure_warn
    max_queue = settings.ws_max_recv_queue

    # Track active queue depth (we drain synchronously; qsize ~ bytes_in / chunk).
    pending = 0

    while True:
        # Exit loop if session has been finalized (CLOSED) or is draining.
        if session.status in (SessionStatus.CLOSED, SessionStatus.DRAINING) or session.end_reason is not None:
            return

        # Heartbeat: send ping every interval.
        now = time.monotonic()
        if now - last_pong >= heartbeat_interval:
            try:
                await ws.send_text(
                    json.dumps({"type": "ping", "ts": int(now * 1000)})
                )
            except Exception:
                return
            last_pong = now

        # Check session timeout (no chunk in N seconds → close).
        # NOTE: last_chunk_at is a wall-clock datetime — compare against
        # epoch seconds, NOT time.monotonic() (R1 bug fixed in WS-3).
        if (
            session.last_chunk_at is not None
            and (time.time() - session.last_chunk_at.timestamp())
            > settings.streaming_session_timeout_sec
        ):
            await ws.close(code=WS_CLOSE_TIMEOUT, reason="session idle timeout")
            session.mark_end(reason="timeout")
            return

        try:
            msg = await asyncio.wait_for(
                ws.receive(),
                timeout=heartbeat_interval,
            )
        except TimeoutError:
            continue  # loop will send another ping
        except WebSocketDisconnect:
            raise

        # Backpressure check (approximate by pending counter).
        if pending > warn_threshold:
            try:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "backpressure",
                            "session_id": session.session_id.value,
                            "queue_depth": pending,
                            "message": "Queue depth exceeded threshold; please slow down",
                        }
                    )
                )
            except Exception:
                return
        if pending > max_queue:
            await ws.close(code=WS_CLOSE_BACKPRESSURE, reason="backpressure overflow")
            session.mark_end(reason="backpressure")
            return

        # Route by frame type.
        if "bytes" in msg and msg["bytes"] is not None:
            pending += 1
            try:
                await _handle_binary(ws, session, msg["bytes"], tag_scheduler)
            finally:
                pending -= 1
        elif "text" in msg and msg["text"] is not None:
            await _handle_text(ws, session, msg["text"], settings, retriever)


async def _handle_binary(
    ws: WebSocket,
    session: StreamSession,
    data: bytes,
    tag_scheduler: StreamingTagScheduler | None = None,
) -> None:
    """Parse [4-byte seq BE][N PCM] and route to StreamSession.on_pcm_chunk.

    Emits metrics for segments / vad resets / ASR latency, and feeds
    confirmed segments to the tag scheduler (T9 + T11).
    """
    if len(data) < 4:
        await _send_error(ws, session, "BAD_FRAME", "binary frame too short")
        return
    seq = struct.unpack(">I", data[:4])[0]
    pcm = data[4:]
    t0 = time.perf_counter()
    async for event in session.on_pcm_chunk(pcm, seq=seq):
        event_type = event.get("type")
        if event_type == "segment_confirmed":
            STREAMING_SEGMENTS_TOTAL.labels(mode="confirmed").inc()
            STREAMING_ASR_LATENCY.observe(time.perf_counter() - t0)
            if tag_scheduler is not None:
                await _maybe_emit_tag_update(ws, session, tag_scheduler, seq)
        elif event_type == "realtime_text":
            STREAMING_SEGMENTS_TOTAL.labels(mode="realtime").inc()
        elif event_type == "vad_reset":
            STREAMING_VAD_RESETS_TOTAL.labels(
                reason=str(event.get("reason", "unknown"))
            ).inc()
        await ws.send_text(json.dumps(event, ensure_ascii=False))


async def _maybe_emit_tag_update(
    ws: WebSocket,
    session: StreamSession,
    tag_scheduler: StreamingTagScheduler,
    seq: int,
) -> None:
    """Feed one confirmed segment to the scheduler; emit ``tags_updated`` on trigger."""
    batch = await tag_scheduler.on_segment_confirmed(seq)
    if batch is None:
        return
    STREAMING_TAG_RECOMPUTES_TOTAL.labels(
        status="error" if batch.error else "ok"
    ).inc()
    event: dict[str, Any] = {
        "type": "tags_updated",
        "session_id": session.session_id.value,
        "recording_id": batch.recording_id,
        "segment_count": len(batch.segment_ids),
        "tags_written": batch.tags_written,
        "timestamp_ms": int(time.time() * 1000),
    }
    if batch.error:
        event["error"] = batch.error
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps(event, ensure_ascii=False))


async def _handle_text(
    ws: WebSocket,
    session: StreamSession,
    text: str,
    settings: Settings,
    retriever: StreamingRetriever | None = None,
) -> None:
    """Parse control JSON frame."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        await _send_error(ws, session, "BAD_JSON", "control frame not JSON")
        return
    if not isinstance(payload, dict):
        await _send_error(ws, session, "BAD_JSON", "control frame not object")
        return
    msg_type = payload.get("type")
    if msg_type == "finalize":
        async for event in session.on_finalize():
            if event.get("type") == "segment_confirmed":
                STREAMING_SEGMENTS_TOTAL.labels(mode="confirmed").inc()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        session.mark_end(reason="normal")
    elif msg_type == "reset":
        async for event in session.on_control_reset():
            STREAMING_VAD_RESETS_TOTAL.labels(reason="client_request").inc()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
    elif msg_type == "query":
        await _handle_query(ws, session, payload, settings, retriever)
    elif msg_type == "pong":
        # Client heartbeat response — no-op (loop tracks last_pong via send).
        pass
    else:
        await _send_error(ws, session, "UNKNOWN_TYPE", f"unknown type={msg_type!r}")


async def _handle_query(
    ws: WebSocket,
    session: StreamSession,
    payload: dict[str, Any],
    settings: Settings,
    retriever: StreamingRetriever | None,
) -> None:
    """Handle a ``query`` control frame → ``retrieval_result`` event (T10)."""
    if not getattr(settings, "enable_streaming_retrieval", False):
        await _send_error(
            ws, session, "RETRIEVAL_DISABLED",
            "streaming retrieval is disabled (enable_streaming_retrieval=False)",
        )
        return
    if retriever is None:
        await _send_error(
            ws, session, "RETRIEVER_UNAVAILABLE",
            "no streaming retriever configured on this server",
        )
        return
    query = str(payload.get("query", "")).strip()
    if not query:
        await _send_error(ws, session, "BAD_QUERY", "query text required")
        return
    top_k_raw = payload.get("top_k", 5)
    top_k = top_k_raw if isinstance(top_k_raw, int) and top_k_raw > 0 else 5
    min_conf_raw = payload.get("min_confidence")
    min_confidence = (
        min_conf_raw
        if min_conf_raw in ("EXTRACTED", "INFERRED", "AMBIGUOUS")
        else None
    )
    try:
        result = await retriever.retrieve(
            query,
            tenant_id=session.tenant_id,
            session_id=session.session_id.value,
            top_k=top_k,
            min_confidence=min_confidence,
        )
    except Exception as exc:
        logger.warning(
            "streaming retrieval failed session=%s: %s",
            session.session_id.value, exc,
        )
        await _send_error(ws, session, "RETRIEVAL_FAILED", str(exc)[:200])
        return
    await ws.send_text(
        json.dumps(
            {
                "type": "retrieval_result",
                "session_id": session.session_id.value,
                "query": query,
                "result": result.to_dict(),
                "timestamp_ms": int(time.time() * 1000),
            },
            ensure_ascii=False,
        )
    )


async def _send_error(
    ws: WebSocket,
    session: StreamSession,
    code: str,
    message: str,
) -> None:
    """Best-effort error event send."""
    with contextlib.suppress(Exception):
        await ws.send_text(
            json.dumps(
                {
                    "type": "error",
                    "session_id": session.session_id.value,
                    "code": code,
                    "message": message,
                    "recoverable": True,
                }
            )
        )


# ------------------------------------------------------------------
# WS-3 helpers — tag scheduler DI
# ------------------------------------------------------------------
def _build_tag_scheduler(
    app: Any,
    settings: Settings,
    user: WSAuthUser,
    recording_id: int,
) -> StreamingTagScheduler | None:
    """Build a per-session StreamingTagScheduler when DI is available.

    Resolution order:
        1. ``app.state.tag_scheduler_factory`` (tests / custom DI) — called
           with ``(tenant_id, recording_id)``.
        2. ``app.state.recompute_service`` (production DI from main.py).
        3. ``None`` — tag scheduling silently disabled (zero regression).
    """
    from audio_graphy.core.streaming_tag_scheduler import StreamingTagScheduler

    factory = getattr(app.state, "tag_scheduler_factory", None)
    if factory is not None:
        try:
            scheduler = factory(user.tenant_id, recording_id)
        except Exception as exc:
            logger.warning("tag_scheduler_factory failed: %s", exc)
            return None
        return scheduler if isinstance(scheduler, StreamingTagScheduler) else None

    recompute_svc = getattr(app.state, "recompute_service", None)
    if recompute_svc is None:
        return None
    return StreamingTagScheduler(
        recompute_svc,
        interval_n=settings.streaming_tag_interval,
        debounce_ms=settings.streaming_tag_debounce_ms,
        tenant_id=user.tenant_id,
        recording_id=recording_id,
    )


# ------------------------------------------------------------------
# Session registry + DB persistence
# ------------------------------------------------------------------
def _register_session(app: Any, session: StreamSession) -> None:
    """Add session to ``app.state.stream_sessions`` (M8 in-memory)."""
    registry = getattr(app.state, "stream_sessions", None)
    if registry is None:
        registry = {}
        app.state.stream_sessions = registry
    registry[session.session_id.value] = session


def _unregister_session(app: Any, session: StreamSession) -> None:
    """Remove session from the registry."""
    registry = getattr(app.state, "stream_sessions", None)
    if registry is None:
        return
    registry.pop(session.session_id.value, None)


async def _persist_session_row(app: Any, session: StreamSession) -> None:
    """Best-effort INSERT into streaming_sessions table.

    Failures are logged + swallowed (audit failure must not roll back
    the WS close). Mirrors ``AuditWriter`` resilience policy.
    """
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        return
    try:
        from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

        async with session_factory() as db:
            db.add(
                StreamingSessionORM(
                    tenant_id=session.tenant_id,
                    session_id=session.session_id.value,
                    recording_id=session.recording_id,
                    user_id=session.user_id,
                    started_at=session.started_at,
                    ended_at=session.last_chunk_at or session.started_at,
                    last_chunk_at=session.last_chunk_at,
                    seg_confirmed_count=session.seg_confirmed_count,
                    seg_realtime_count=session.seg_realtime_count,
                    bytes_in=session.bytes_in,
                    error_count=session.error_count,
                    end_reason=session.end_reason,
                    consent_token_hash=session.consent_token_hash,
                    stats=session.stats(),
                )
            )
            await db.commit()
    except Exception as exc:
        logger.warning("streaming_sessions insert failed: %s", exc)
