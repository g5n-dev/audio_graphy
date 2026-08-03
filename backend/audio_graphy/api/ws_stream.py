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

Auth: short-lived, single-use capability via ``?ticket=``. A legacy JWT query
path exists only behind an explicit emergency compatibility setting.
Tenant: bound into the consumed ticket.
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
import hashlib
import json
import logging
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.adapters.protocols import StreamSessionId
from audio_graphy.api.deps import get_current_user, get_db
from audio_graphy.api.metrics import (
    STREAMING_ASR_LATENCY,
    STREAMING_SEGMENTS_TOTAL,
    STREAMING_SESSIONS_ACTIVE,
    STREAMING_SESSIONS_TOTAL,
    STREAMING_TAG_RECOMPUTES_TOTAL,
    STREAMING_VAD_RESETS_TOTAL,
)
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_role
from audio_graphy.auth.ws_auth import WS_AUTH_FAILED_CODE, WSAuthUser, verify_ws_token
from audio_graphy.core.stream_session import (
    DEFAULT_CONFIRMED_FLUSH_THRESHOLD,
    DEFAULT_REALTIME_WINDOW,
    SessionStatus,
    StreamSession,
    hash_consent_token,
)
from audio_graphy.errors import EntityNotFoundError

if TYPE_CHECKING:
    from audio_graphy.config import Settings
    from audio_graphy.core.streaming_retrieval import StreamingRetriever
    from audio_graphy.core.streaming_tag_scheduler import StreamingTagScheduler
    from audio_graphy.services.streaming_durability import StreamingDurabilityWriter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

# WS close codes (architecture §6.1.3 + §6.1.4).
WS_CLOSE_NORMAL: int = 1000
WS_CLOSE_BACKPRESSURE: int = 1011
WS_CLOSE_CONSENT_MISSING: int = 4002
WS_CLOSE_TIMEOUT: int = 4003


class StreamingTicketRequest(BaseModel):
    recording_id: int = Field(gt=0)
    consent_token: str = Field(min_length=1, max_length=512)


class StreamingTicketResponse(BaseModel):
    ticket: str
    expires_at: str
    ws_url: str


@router.post(
    "/ws/tickets",
    response_model=StreamingTicketResponse,
    status_code=201,
    dependencies=[Depends(require_role("admin", "inspector", "agent"))],
)
@router.post(
    "/api/v1/ws/tickets",
    response_model=StreamingTicketResponse,
    status_code=201,
    dependencies=[Depends(require_role("admin", "inspector", "agent"))],
    include_in_schema=False,
)
async def create_streaming_ticket(
    body: StreamingTicketRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> StreamingTicketResponse:
    """Exchange authenticated HTTP authority for a 60-second WS capability."""
    from audio_graphy.core.ws_ticket import WSTicketError, issue_ws_ticket

    settings: Settings = request.app.state.settings
    try:
        issued = await issue_ws_ticket(
            db,
            tenant_id=user.tenant_id,
            recording_id=body.recording_id,
            user_id=user.id,
            role=user.role,
            consent_token_hash=hash_consent_token(body.consent_token),
            ttl_sec=int(settings.streaming_ws_ticket_ttl_sec),
        )
    except WSTicketError as exc:
        # Deliberately hide whether a foreign-tenant recording exists.
        raise EntityNotFoundError(
            message="Recording is not available for streaming",
            detail={"recording_id": body.recording_id},
        ) from exc
    return StreamingTicketResponse(
        ticket=issued.token,
        expires_at=issued.expires_at.isoformat(),
        ws_url=f"/ws/stream?ticket={issued.token}",
    )


@router.websocket("/ws/stream")
async def ws_stream(
    ws: WebSocket,
    ticket: str | None = Query(
        default=None,
        description="Short-lived, single-use streaming ticket",
    ),
    token: str | None = Query(
        default=None,
        description="Legacy JWT compatibility path",
    ),
) -> None:
    """Main streaming endpoint — PCM in, server events out.

    See module docstring for protocol details.
    """
    # --- 1. Resolve settings + JWT manager from app state ---
    settings: Settings = ws.app.state.settings
    jwt_manager: JWTManager = ws.app.state.jwt_manager

    # --- 2. Authenticate with a one-time ticket; JWT is emergency-only ---
    ticket_binding: Any | None = None
    if ticket and token:
        raise WebSocketException(
            code=WS_AUTH_FAILED_CODE,
            reason="ambiguous websocket credentials",
        )
    if ticket:
        from audio_graphy.core.ws_ticket import WSTicketError, consume_ws_ticket

        session_factory = getattr(ws.app.state, "session_factory", None)
        if session_factory is None:
            raise WebSocketException(
                code=WS_AUTH_FAILED_CODE,
                reason="ticket store unavailable",
            )
        try:
            ticket_binding = await consume_ws_ticket(session_factory, ticket)
        except WSTicketError as exc:
            raise WebSocketException(
                code=WS_AUTH_FAILED_CODE,
                reason=str(exc),
            ) from exc
        user = WSAuthUser(
            user_id=ticket_binding.user_id,
            tenant_id=ticket_binding.tenant_id,
            role=ticket_binding.role,
        )
    elif settings.streaming_allow_legacy_jwt_query:
        user = await verify_ws_token(token or "", jwt_manager)
    else:
        raise WebSocketException(
            code=WS_AUTH_FAILED_CODE,
            reason="one-time websocket ticket required",
        )

    # --- 3. Accept the WS after auth (so 4001 closes cleanly) ---
    await ws.accept()
    logger.info(
        "WS /ws/stream accepted user=%s tenant=%s",
        user.user_id,
        user.tenant_id,
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
    if ticket_binding is not None and recording_id_raw != ticket_binding.recording_id:
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="ticket recording mismatch")
        return

    consent_token = str(init_payload.get("consent_token", "")).strip()
    if not consent_token:
        # PRD §5.3 R10 — missing consent closes the connection + audit.
        logger.warning(
            "WS /ws/stream consent missing session=%s tenant=%s",
            session_id_value,
            user.tenant_id,
        )
        await ws.close(code=WS_CLOSE_CONSENT_MISSING, reason="consent_token required")
        return
    consent_hash = hash_consent_token(consent_token)
    if ticket_binding is not None and consent_hash != ticket_binding.consent_token_hash:
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="ticket consent mismatch")
        return
    resume_requested = "resume_from_seq" in init_payload
    resume_from_seq = init_payload.get("resume_from_seq", 0)
    if (
        isinstance(resume_from_seq, bool)
        or not isinstance(resume_from_seq, int)
        or resume_from_seq < 0
    ):
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="invalid resume_from_seq")
        return
    resume_token_raw = init_payload.get("resume_token")
    if resume_token_raw is not None and (
        not isinstance(resume_token_raw, str) or not resume_token_raw or len(resume_token_raw) > 128
    ):
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="invalid resume_token")
        return
    resume_token = resume_token_raw if isinstance(resume_token_raw, str) else None

    try:
        (
            persistence_id,
            epoch,
            generation,
            pipeline_run_id,
            lease_token,
        ) = await _reserve_session_row(
            ws.app,
            tenant_id=user.tenant_id,
            session_id=session_id_value,
            recording_id=recording_id_raw,
            user_id=user.user_id,
            consent_token_hash=consent_hash,
            timeout_sec=settings.streaming_session_timeout_sec,
            resume_from_seq=resume_from_seq,
            resume_requested=resume_requested,
            resume_token=resume_token,
        )
    except Exception as exc:
        logger.warning("streaming session reservation failed: %s", exc)
        await ws.close(code=WS_AUTH_FAILED_CODE, reason="recording/session unavailable")
        return

    # --- 5. Build streaming session (adapters from bundle factory) ---
    from audio_graphy.adapters.bundle import (
        acquire_streaming_adapters_for_session,
    )

    # Hotwords from tenant entity_aliases (M8: leave empty; WS-3 will load).
    hotwords: tuple[str, ...] = ()
    app_pool = getattr(ws.app.state, "streaming_pool", None)

    streaming_bundle: Any | None = None
    session: StreamSession | None = None
    try:
        streaming_bundle = await acquire_streaming_adapters_for_session(
            settings,
            session_id=session_id_value,
            tenant_id=user.tenant_id,
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
            close_asr_on_finalize=settings.adapter_streaming_asr_mode != "real",
            epoch=epoch,
            generation=generation,
            pipeline_run_id=pipeline_run_id,
            lease_token=lease_token,
            lease_ttl_seconds=max(
                1.0,
                float(settings.streaming_session_timeout_sec),
            ),
            persistence_id=persistence_id,
        )
        _register_session(ws.app, session)
        session.mark_active()
        await _mark_session_row_active(ws.app, session)
    except Exception as exc:
        logger.exception(
            "streaming adapter/session activation failed session=%s: %s",
            session_id_value,
            exc,
        )
        await _fail_reserved_session(
            ws.app,
            persistence_id=persistence_id,
            tenant_id=user.tenant_id,
            pipeline_run_id=pipeline_run_id,
            reason=str(exc),
        )
        if session is not None:
            _unregister_session(ws.app, session)
        await _release_unopened_streaming_bundle(
            streaming_bundle,
            real_asr=settings.adapter_streaming_asr_mode == "real",
            pool=app_pool,
        )
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason="streaming adapters unavailable")
        return

    assert session is not None
    assert streaming_bundle is not None
    durability_writer: StreamingDurabilityWriter | None = None
    if persistence_id is not None:
        from audio_graphy.services.streaming_durability import (
            StreamingDurabilityWriter,
        )

        durability_writer = StreamingDurabilityWriter(
            ws.app.state.session_factory,
            pii_scrubber=getattr(ws.app.state, "pii_scrubber", None),
        )

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
                "epoch": session.epoch,
                "generation": session.generation,
                "resume_from_seq": resume_from_seq,
                "resume_token": session.lease_token,
                "capabilities": {
                    "max_buffer_chunks": settings.ws_max_recv_queue,
                    "vad_reset_strategy": f"seq_gap_{settings.streaming_vad_reset_seq_gap}",
                },
            },
            ensure_ascii=False,
        )
    )
    if durability_writer is not None:
        for staged in await durability_writer.pending_frames(session):
            frame = struct.pack(">I", staged.source_seq) + staged.pcm
            await _handle_binary(
                ws,
                session,
                frame,
                tag_scheduler,
                durability_writer,
            )
            if session.status == SessionStatus.DRAINING:
                break

    # --- 7. Receive / heartbeat loop ---
    try:
        await _run_recv_loop(
            ws,
            session,
            settings,
            user,
            tag_scheduler,
            retriever,
            durability_writer,
        )
    except WebSocketDisconnect:
        logger.info("WS /ws/stream client disconnect session=%s", session_id_value)
        session.begin_drain(reason="client_disconnect")
    except Exception as exc:
        logger.exception("WS /ws/stream error session=%s: %s", session_id_value, exc)
        session.begin_drain(reason="error")
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
        # --- 8. Drain adapters, flush tags, persist, and return pooled ASR ---
        with contextlib.suppress(Exception):
            await _mark_session_row_status(ws.app, session, "DRAINING")
        if session.status != SessionStatus.CLOSED:
            with contextlib.suppress(Exception):
                async for event in session.on_finalize():
                    await _send_stream_event(
                        ws,
                        session,
                        event,
                        durability_writer,
                        tag_scheduler,
                    )
        with contextlib.suppress(Exception):
            await _mark_session_row_status(ws.app, session, "COMMITTING")
        if tag_scheduler is not None:
            with contextlib.suppress(Exception):
                flushed = await tag_scheduler.flush()
                if flushed is not None:
                    STREAMING_TAG_RECOMPUTES_TOTAL.labels(
                        status="error" if flushed.error else "ok"
                    ).inc()
        STREAMING_SESSIONS_ACTIVE.dec()
        persisted = await _persist_session_row(ws.app, session)
        if persisted:
            with contextlib.suppress(Exception):
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
        else:
            session.end_reason = "error"
            session.error_count += 1
            await _send_error(
                ws,
                session,
                "SESSION_COMMIT_FAILED",
                "streaming session could not be committed",
            )
        _unregister_session(ws.app, session)
        if settings.adapter_streaming_asr_mode == "real" and app_pool is not None:
            with contextlib.suppress(Exception):
                await app_pool.release(streaming_bundle.asr)
        with contextlib.suppress(Exception):
            await ws.close(
                code=WS_CLOSE_NORMAL if persisted else WS_CLOSE_BACKPRESSURE,
            )


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
    durability_writer: StreamingDurabilityWriter | None = None,
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
        if (
            session.status in (SessionStatus.CLOSED, SessionStatus.DRAINING)
            or session.end_reason is not None
        ):
            return

        # Heartbeat: send ping every interval.
        now = time.monotonic()
        if now - last_pong >= heartbeat_interval:
            try:
                await ws.send_text(json.dumps({"type": "ping", "ts": int(now * 1000)}))
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
            session.begin_drain(reason="timeout")
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
            session.begin_drain(reason="backpressure")
            return

        # Route by frame type.
        if "bytes" in msg and msg["bytes"] is not None:
            pending += 1
            try:
                await _handle_binary(
                    ws,
                    session,
                    msg["bytes"],
                    tag_scheduler,
                    durability_writer,
                )
            finally:
                pending -= 1
        elif "text" in msg and msg["text"] is not None:
            await _handle_text(
                ws,
                session,
                msg["text"],
                settings,
                retriever,
                tag_scheduler,
                durability_writer,
            )


async def _handle_binary(
    ws: WebSocket,
    session: StreamSession,
    data: bytes,
    tag_scheduler: StreamingTagScheduler | None = None,
    durability_writer: StreamingDurabilityWriter | None = None,
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
    if durability_writer is not None:
        try:
            staged = await durability_writer.stage_frame(
                session,
                source_seq=seq,
                pcm=pcm,
            )
        except Exception as exc:
            logger.exception(
                "streaming PCM staging failed session=%s seq=%s: %s",
                session.session_id.value,
                seq,
                exc,
            )
            session.error_count += 1
            session.begin_drain(reason="error")
            await _send_error(
                ws,
                session,
                "FRAME_STAGING_FAILED",
                "PCM frame could not be staged",
            )
            return
        if staged.state == "CONSUMED":
            await ws.send_text(
                json.dumps(
                    {
                        "type": "frame_ack",
                        "session_id": session.session_id.value,
                        "seq": seq,
                        "duplicate": True,
                    },
                    ensure_ascii=False,
                )
            )
            return
    t0 = time.perf_counter()
    async for event in session.on_pcm_chunk(pcm, seq=seq):
        event_type = event.get("type")
        if event_type == "segment_confirmed":
            STREAMING_ASR_LATENCY.observe(time.perf_counter() - t0)
        elif event_type == "realtime_text":
            STREAMING_SEGMENTS_TOTAL.labels(mode="realtime").inc()
        elif event_type == "vad_reset":
            STREAMING_VAD_RESETS_TOTAL.labels(reason=str(event.get("reason", "unknown"))).inc()
        sent = await _send_stream_event(
            ws,
            session,
            event,
            durability_writer,
            tag_scheduler,
        )
        if not sent:
            return


async def _maybe_emit_tag_update(
    ws: WebSocket,
    session: StreamSession,
    tag_scheduler: StreamingTagScheduler,
    segment_id: int,
) -> None:
    """Feed one confirmed segment to the scheduler; emit ``tags_updated`` on trigger."""
    batch = await tag_scheduler.on_segment_confirmed(segment_id)
    if batch is None:
        return
    STREAMING_TAG_RECOMPUTES_TOTAL.labels(status="error" if batch.error else "ok").inc()
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


async def _send_stream_event(
    ws: WebSocket,
    session: StreamSession,
    event: dict[str, Any],
    durability_writer: StreamingDurabilityWriter | None,
    tag_scheduler: StreamingTagScheduler | None,
) -> bool:
    """Make confirmed speech durable before exposing it as confirmed."""

    if event.get("type") == "segment_confirmed":
        if durability_writer is None:
            session.error_count += 1
            session.begin_drain(reason="error")
            await _send_error(
                ws,
                session,
                "DURABILITY_UNAVAILABLE",
                "confirmed speech could not be persisted",
            )
            return False
        try:
            durable = await durability_writer.persist_confirmed(session, event)
        except Exception as exc:
            logger.exception(
                "streaming durable segment failed session=%s: %s",
                session.session_id.value,
                exc,
            )
            session.error_count += 1
            session.begin_drain(reason="error")
            await _send_error(
                ws,
                session,
                "DURABILITY_FAILED",
                "confirmed speech could not be persisted",
            )
            return False
        event = {
            **event,
            "segment_id": durable.segment_id,
            "chunk_id": durable.chunk_id,
            "generation": durable.generation,
            "durable": True,
        }
        STREAMING_SEGMENTS_TOTAL.labels(mode="confirmed").inc()
        if tag_scheduler is not None:
            await _maybe_emit_tag_update(
                ws,
                session,
                tag_scheduler,
                durable.segment_id,
            )
    await ws.send_text(json.dumps(event, ensure_ascii=False))
    return True


async def _handle_text(
    ws: WebSocket,
    session: StreamSession,
    text: str,
    settings: Settings,
    retriever: StreamingRetriever | None = None,
    tag_scheduler: StreamingTagScheduler | None = None,
    durability_writer: StreamingDurabilityWriter | None = None,
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
            await _send_stream_event(
                ws,
                session,
                event,
                durability_writer,
                tag_scheduler,
            )
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
            ws,
            session,
            "RETRIEVAL_DISABLED",
            "streaming retrieval is disabled (enable_streaming_retrieval=False)",
        )
        return
    if retriever is None:
        await _send_error(
            ws,
            session,
            "RETRIEVER_UNAVAILABLE",
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
        min_conf_raw if min_conf_raw in ("EXTRACTED", "INFERRED", "AMBIGUOUS") else None
    )
    try:
        result = await retriever.retrieve(
            query,
            tenant_id=session.tenant_id,
            session_id=session.session_id.value,
            top_k=top_k,
            min_confidence=min_confidence,
            permission_scope={
                "actor_user_id": session.user_id,
                "recording_id": session.recording_id,
            },
        )
    except Exception as exc:
        logger.warning(
            "streaming retrieval failed session=%s: %s",
            session.session_id.value,
            exc,
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
async def _release_unopened_streaming_bundle(
    bundle: Any | None,
    *,
    real_asr: bool,
    pool: Any | None,
) -> None:
    if bundle is None:
        return
    with contextlib.suppress(Exception):
        await bundle.vad.aclose()
    if real_asr and pool is not None:
        with contextlib.suppress(Exception):
            await pool.release(bundle.asr)
    else:
        with contextlib.suppress(Exception):
            await bundle.asr.aclose()


async def _fail_reserved_session(
    app: Any,
    *,
    persistence_id: int | None,
    tenant_id: str,
    pipeline_run_id: int | None,
    reason: str,
) -> None:
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None or persistence_id is None:
        return
    from audio_graphy.models.pipeline import RecordingPipelineRun
    from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

    async with session_factory() as db, db.begin():
        row = await db.get(
            StreamingSessionORM,
            persistence_id,
            with_for_update=True,
        )
        if row is not None and str(row.tenant_id) == tenant_id:
            row.status = "FAILED"
            row.end_reason = "error"
            row.error_count = int(row.error_count) + 1
            row.ended_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_expires_at = None
            row.stats = {"activation_error": reason[:500]}
        if pipeline_run_id is not None:
            run = await db.get(
                RecordingPipelineRun,
                pipeline_run_id,
                with_for_update=True,
            )
            if (
                run is not None
                and str(run.tenant_id) == tenant_id
                and run.state
                not in {
                    "ready",
                    "ready_no_speech",
                    "failed_terminal",
                    "superseded",
                }
            ):
                run.state = "failed_retryable"
                run.error_code = "STREAM_ACTIVATION_FAILED"
                run.error_message = reason[:1000]


async def _reserve_session_row(
    app: Any,
    *,
    tenant_id: str,
    session_id: str,
    recording_id: int,
    user_id: int | None,
    consent_token_hash: str,
    timeout_sec: float,
    resume_from_seq: int,
    resume_requested: bool,
    resume_token: str | None,
) -> tuple[int | None, int, int, int | None, str | None]:
    """Persist ``RESERVING`` before any stateful streaming work begins."""
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        return None, 1, 1, None, None

    from audio_graphy.models.pipeline import RecordingPipelineRun
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.streaming_pcm_frame import StreamingPCMFrame
    from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

    started_at = datetime.now(UTC)
    async with session_factory() as db, db.begin():
        recording = (
            await db.execute(
                select(Recording)
                .where(
                    Recording.id == recording_id,
                    Recording.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if recording is None:
            raise ValueError("recording not found in tenant")

        latest_session = (
            await db.execute(
                select(StreamingSessionORM)
                .where(
                    StreamingSessionORM.tenant_id == tenant_id,
                    StreamingSessionORM.session_id == session_id,
                )
                .order_by(StreamingSessionORM.epoch.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if latest_session is not None:
            if not resume_requested:
                raise ValueError("streaming session already exists; resume is required")
            if latest_session.recording_id != recording_id:
                raise ValueError("streaming session cannot cross recordings")
            if latest_session.status == "CLOSED":
                raise ValueError("streaming session is already closed")
            if resume_from_seq > int(latest_session.ack_seq_high_watermark) + 1:
                raise ValueError("resume sequence is ahead of the durable watermark")
            if latest_session.status in {
                "RESERVING",
                "ACTIVE",
                "DRAINING",
                "COMMITTING",
            }:
                if (
                    latest_session.lease_token is None
                    or resume_token is None
                    or not secrets.compare_digest(
                        latest_session.lease_token,
                        resume_token,
                    )
                ):
                    raise ValueError("active streaming lease cannot be preempted")
                latest_session.status = "INCOMPLETE"
                latest_session.end_reason = "client_disconnect"
                latest_session.ended_at = started_at
                latest_session.lease_token = None
                latest_session.lease_expires_at = None
            epoch = int(latest_session.epoch) + 1
        else:
            epoch = 1
        run_idempotency_key = f"stream:{session_id}"
        run = (
            await db.execute(
                select(RecordingPipelineRun)
                .where(
                    RecordingPipelineRun.tenant_id == tenant_id,
                    RecordingPipelineRun.recording_id == recording_id,
                    RecordingPipelineRun.idempotency_key == run_idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            max_generation = int(
                (
                    await db.execute(
                        select(func.max(RecordingPipelineRun.generation)).where(
                            RecordingPipelineRun.tenant_id == tenant_id,
                            RecordingPipelineRun.recording_id == recording_id,
                        )
                    )
                ).scalar_one_or_none()
                or 0
            )
            generation = max_generation + 1
            source_fingerprint = hashlib.sha256(
                f"stream:{tenant_id}:{recording_id}:{session_id}".encode()
            ).hexdigest()
            config_fingerprint = hashlib.sha256(b"streaming-v1:pcm-s16le:16000:mono").hexdigest()
            run = RecordingPipelineRun(
                tenant_id=tenant_id,
                recording_id=recording_id,
                generation=generation,
                idempotency_key=run_idempotency_key,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                state="asr",
                attempt_count=1,
                required_projections=["vector", "graph", "file_index", "tag"],
                completed_projections=[],
                started_at=started_at,
            )
            db.add(run)
            await db.flush()
        elif run.state in {
            "ready",
            "ready_no_speech",
            "failed_terminal",
            "superseded",
        }:
            raise ValueError("streaming session is already terminal")
        generation = int(run.generation)
        max_acknowledged_seq = (
            await db.execute(
                select(func.max(StreamingPCMFrame.source_seq)).where(
                    StreamingPCMFrame.tenant_id == tenant_id,
                    StreamingPCMFrame.session_key == session_id,
                    StreamingPCMFrame.recording_id == recording_id,
                )
            )
        ).scalar_one_or_none()
        acknowledged_seq = int(max_acknowledged_seq) if max_acknowledged_seq is not None else -1

        session_lease_token = secrets.token_hex(16)
        row = StreamingSessionORM(
            tenant_id=tenant_id,
            session_id=session_id,
            epoch=epoch,
            status="RESERVING",
            generation=generation,
            pipeline_run_id=int(run.id),
            ack_seq_high_watermark=acknowledged_seq,
            durable_segment_high_watermark=0,
            lease_expires_at=started_at + timedelta(seconds=max(1.0, float(timeout_sec))),
            lease_token=session_lease_token,
            recording_id=recording_id,
            user_id=user_id,
            started_at=started_at,
            ended_at=None,
            last_chunk_at=None,
            seg_confirmed_count=0,
            seg_realtime_count=0,
            bytes_in=0,
            error_count=0,
            end_reason=None,
            consent_token_hash=consent_token_hash,
            stats=None,
        )
        db.add(row)
        await db.flush()
        return int(row.id), epoch, generation, int(run.id), session_lease_token


async def _mark_session_row_active(app: Any, session: StreamSession) -> None:
    """Move a reserved row to ACTIVE after adapters are ready."""
    if session.persistence_id is None:
        return
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        return
    from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

    async with session_factory() as db, db.begin():
        row = await db.get(
            StreamingSessionORM,
            session.persistence_id,
            with_for_update=True,
        )
        if (
            row is None
            or str(row.tenant_id) != session.tenant_id
            or row.lease_token != session.lease_token
            or row.status != "RESERVING"
        ):
            raise RuntimeError("reserved streaming session disappeared")
        row.status = "ACTIVE"
        row.lease_expires_at = _streaming_lease_deadline(session)


async def _mark_session_row_status(
    app: Any,
    session: StreamSession,
    status: str,
) -> None:
    if session.persistence_id is None:
        return
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        return
    from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

    async with session_factory() as db, db.begin():
        row = await db.get(
            StreamingSessionORM,
            session.persistence_id,
            with_for_update=True,
        )
        if (
            row is None
            or str(row.tenant_id) != session.tenant_id
            or row.lease_token != session.lease_token
        ):
            raise RuntimeError("reserved streaming session disappeared")
        legal_predecessors = {
            "DRAINING": {"ACTIVE", "DRAINING"},
            "COMMITTING": {"DRAINING", "COMMITTING"},
        }
        if status not in legal_predecessors or row.status not in legal_predecessors[status]:
            raise RuntimeError(f"illegal streaming session transition {row.status} -> {status}")
        row.status = status
        row.lease_expires_at = _streaming_lease_deadline(session)


def _register_session(app: Any, session: StreamSession) -> None:
    """Add session to ``app.state.stream_sessions`` (M8 in-memory)."""
    registry = getattr(app.state, "stream_sessions", None)
    if registry is None:
        registry = {}
        app.state.stream_sessions = registry
    registry[(session.tenant_id, session.session_id.value, session.epoch)] = session


def _unregister_session(app: Any, session: StreamSession) -> None:
    """Remove session from the registry."""
    registry = getattr(app.state, "stream_sessions", None)
    if registry is None:
        return
    registry.pop((session.tenant_id, session.session_id.value, session.epoch), None)


async def _persist_session_row(app: Any, session: StreamSession) -> bool:
    """Commit the terminal session/run state; false forbids ``session_closed``."""
    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        return True
    try:
        from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

        async with session_factory() as db:
            values = {
                "tenant_id": session.tenant_id,
                "session_id": session.session_id.value,
                "recording_id": session.recording_id,
                "user_id": session.user_id,
                "started_at": session.started_at,
                "epoch": session.epoch,
                "status": (
                    "CLOSED"
                    if session.end_reason == "normal"
                    else ("FAILED" if session.end_reason == "error" else "INCOMPLETE")
                ),
                "generation": session.generation,
                "pipeline_run_id": session.pipeline_run_id,
                "ack_seq_high_watermark": session.last_seq,
                "durable_segment_high_watermark": (session.durable_segment_high_watermark),
                "lease_token": None,
                "lease_expires_at": None,
                "ended_at": datetime.now(UTC),
                "last_chunk_at": session.last_chunk_at,
                "seg_confirmed_count": session.seg_confirmed_count,
                "seg_realtime_count": session.seg_realtime_count,
                "bytes_in": session.bytes_in,
                "error_count": session.error_count,
                "end_reason": session.end_reason,
                "consent_token_hash": session.consent_token_hash,
                "stats": session.stats(),
            }
            if session.persistence_id is not None:
                row = await db.get(
                    StreamingSessionORM,
                    session.persistence_id,
                    with_for_update=True,
                )
                if (
                    row is None
                    or str(row.tenant_id) != session.tenant_id
                    or row.lease_token != session.lease_token
                    or row.status != "COMMITTING"
                ):
                    raise RuntimeError("reserved streaming session disappeared")
                for key, value in values.items():
                    setattr(row, key, value)
            else:
                db.add(
                    StreamingSessionORM(
                        **values,
                    )
                )
            if session.pipeline_run_id is not None:
                from audio_graphy.models.pipeline import RecordingPipelineRun

                run = await db.get(
                    RecordingPipelineRun,
                    session.pipeline_run_id,
                    with_for_update=True,
                )
                if (
                    run is not None
                    and run.tenant_id == session.tenant_id
                    and run.recording_id == session.recording_id
                    and run.state
                    not in {
                        "ready",
                        "ready_no_speech",
                        "failed_terminal",
                        "superseded",
                    }
                ):
                    if session.end_reason == "normal":
                        if session.durable_segment_high_watermark > 0:
                            run.state = "projections"
                        else:
                            run.state = "partial"
                            run.error_code = "NO_DURABLE_SPEECH"
                            run.error_message = "stream closed without a durable confirmed segment"
                    else:
                        run.state = "failed_retryable"
                        run.error_code = "STREAM_INCOMPLETE"
                        run.error_message = f"stream ended before commit: {session.end_reason}"
            await db.commit()
        return True
    except Exception as exc:
        logger.warning("streaming_sessions insert failed: %s", exc)
        return False


def _streaming_lease_deadline(session: StreamSession) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=max(1.0, float(session.lease_ttl_seconds)))
