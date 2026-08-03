"""Transactional persistence for confirmed WebSocket speech segments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.pii import PIIScrubber
from audio_graphy.core.stream_session import StreamSession
from audio_graphy.models.chunk import Chunk, ChunkSegment
from audio_graphy.models.pipeline import ProjectionOutbox, RecordingPipelineRun
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.streaming_pcm_frame import StreamingPCMFrame
from audio_graphy.models.streaming_segment_receipt import StreamingSegmentReceipt
from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM

_REQUIRED_PROJECTIONS = ("vector", "graph", "file_index", "tag")


@dataclass(frozen=True, slots=True)
class DurableStreamingSegment:
    segment_id: int
    chunk_id: int
    generation: int
    source_seq: int


@dataclass(frozen=True, slots=True)
class StagedPCMFrame:
    source_seq: int
    pcm: bytes
    state: str
    duplicate: bool


class StreamingDurabilityWriter:
    """Persist Segment, Chunk, normalized lineage and outbox before ACK."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pii_scrubber: PIIScrubber | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._pii_scrubber = pii_scrubber or PIIScrubber()

    async def stage_frame(
        self,
        session: StreamSession,
        *,
        source_seq: int,
        pcm: bytes,
    ) -> StagedPCMFrame:
        """Commit transport input before the server emits ``frame_ack``."""

        if session.persistence_id is None:
            raise RuntimeError("streaming session has no durable reservation")
        if source_seq < 0 or not pcm or len(pcm) > 65_536:
            raise ValueError("streaming PCM frame is invalid")
        async with self._session_factory() as db, db.begin():
            session_row = (
                await db.execute(
                    select(StreamingSessionORM)
                    .where(
                        StreamingSessionORM.id == session.persistence_id,
                        StreamingSessionORM.tenant_id == session.tenant_id,
                        StreamingSessionORM.recording_id == session.recording_id,
                        StreamingSessionORM.lease_token == session.lease_token,
                        StreamingSessionORM.status == "ACTIVE",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if session_row is None:
                raise RuntimeError("streaming reservation lease was lost")
            session_row.lease_expires_at = _lease_deadline(session)
            existing = (
                await db.execute(
                    select(StreamingPCMFrame)
                    .where(
                        StreamingPCMFrame.tenant_id == session.tenant_id,
                        StreamingPCMFrame.session_key == session.session_id.value,
                        StreamingPCMFrame.source_seq == source_seq,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.recording_id != session.recording_id or bytes(existing.pcm) != pcm:
                    raise RuntimeError("a streaming sequence was replayed with different audio")
                session_row.ack_seq_high_watermark = max(
                    int(session_row.ack_seq_high_watermark),
                    source_seq,
                )
                return StagedPCMFrame(
                    source_seq=source_seq,
                    pcm=bytes(existing.pcm),
                    state=str(existing.state),
                    duplicate=True,
                )
            db.add(
                StreamingPCMFrame(
                    tenant_id=session.tenant_id,
                    session_key=session.session_id.value,
                    recording_id=session.recording_id,
                    source_seq=source_seq,
                    pcm=pcm,
                    state="ACCEPTED",
                )
            )
            session_row.ack_seq_high_watermark = max(
                int(session_row.ack_seq_high_watermark),
                source_seq,
            )
        return StagedPCMFrame(
            source_seq=source_seq,
            pcm=pcm,
            state="ACCEPTED",
            duplicate=False,
        )

    async def pending_frames(self, session: StreamSession) -> list[StagedPCMFrame]:
        """Load unconsumed frames for deterministic reconnect replay."""

        if session.persistence_id is None:
            raise RuntimeError("streaming session has no durable reservation")
        async with self._session_factory() as db, db.begin():
            session_row = (
                await db.execute(
                    select(StreamingSessionORM)
                    .where(
                        StreamingSessionORM.id == session.persistence_id,
                        StreamingSessionORM.tenant_id == session.tenant_id,
                        StreamingSessionORM.recording_id == session.recording_id,
                        StreamingSessionORM.lease_token == session.lease_token,
                        StreamingSessionORM.status == "ACTIVE",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if session_row is None:
                raise RuntimeError("streaming reservation lease was lost")
            session_row.lease_expires_at = _lease_deadline(session)
            rows = list(
                (
                    await db.execute(
                        select(StreamingPCMFrame)
                        .where(
                            StreamingPCMFrame.tenant_id == session.tenant_id,
                            StreamingPCMFrame.session_key == session.session_id.value,
                            StreamingPCMFrame.recording_id == session.recording_id,
                            StreamingPCMFrame.state == "ACCEPTED",
                        )
                        .order_by(StreamingPCMFrame.source_seq)
                    )
                ).scalars()
            )
        return [
            StagedPCMFrame(
                source_seq=int(row.source_seq),
                pcm=bytes(row.pcm),
                state=str(row.state),
                duplicate=True,
            )
            for row in rows
        ]

    async def persist_confirmed(
        self,
        session: StreamSession,
        event: dict[str, Any],
    ) -> DurableStreamingSegment:
        if session.persistence_id is None:
            raise RuntimeError("streaming session has no durable reservation")
        segment_geometry = event.get("segment")
        if not isinstance(segment_geometry, dict):
            raise ValueError("confirmed event has no segment geometry")
        source_seq = event.get("seq")
        if isinstance(source_seq, bool) or not isinstance(source_seq, int) or source_seq < 0:
            raise ValueError("confirmed event has no stable source sequence")
        sentence_id = event.get("sentence_id")
        source_event_key = f"{source_seq}:{sentence_id!s}"[:128]
        text = str(segment_geometry.get("transcript") or event.get("text") or "").strip()
        if not text:
            raise ValueError("confirmed event transcript is empty")
        start_sec = _finite_float(segment_geometry.get("start_sec"), "start_sec")
        end_sec = _finite_float(segment_geometry.get("end_sec"), "end_sec")
        if start_sec < 0 or end_sec <= start_sec:
            raise ValueError("confirmed event geometry is invalid")
        scrubbed_text = self._pii_scrubber.scrub(text).text

        async with self._session_factory() as db, db.begin():
            session_row = (
                await db.execute(
                    select(StreamingSessionORM)
                    .where(
                        StreamingSessionORM.id == session.persistence_id,
                        StreamingSessionORM.tenant_id == session.tenant_id,
                        StreamingSessionORM.recording_id == session.recording_id,
                        StreamingSessionORM.lease_token == session.lease_token,
                        StreamingSessionORM.status.in_(("ACTIVE", "DRAINING", "COMMITTING")),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if session_row is None:
                raise RuntimeError("streaming reservation lease was lost")
            session_row.lease_expires_at = _lease_deadline(session)
            recording = (
                await db.execute(
                    select(Recording)
                    .where(
                        Recording.id == session.recording_id,
                        Recording.tenant_id == session.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if recording is None:
                raise RuntimeError("streaming recording disappeared")

            existing = (
                await db.execute(
                    select(StreamingSegmentReceipt).where(
                        StreamingSegmentReceipt.tenant_id == session.tenant_id,
                        StreamingSegmentReceipt.session_key == session.session_id.value,
                        StreamingSegmentReceipt.source_event_key == source_event_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.recording_id != session.recording_id:
                    raise RuntimeError("streaming event receipt crossed recordings")
                session.durable_segment_high_watermark = max(
                    session.durable_segment_high_watermark,
                    int(existing.segment_id),
                )
                await db.execute(
                    update(StreamingPCMFrame)
                    .where(
                        StreamingPCMFrame.tenant_id == session.tenant_id,
                        StreamingPCMFrame.session_key == session.session_id.value,
                        StreamingPCMFrame.recording_id == session.recording_id,
                        StreamingPCMFrame.source_seq <= source_seq,
                        StreamingPCMFrame.state == "ACCEPTED",
                    )
                    .values(
                        state="CONSUMED",
                        consumed_segment_id=int(existing.segment_id),
                    )
                )
                return DurableStreamingSegment(
                    segment_id=int(existing.segment_id),
                    chunk_id=int(existing.chunk_id),
                    generation=int(existing.generation),
                    source_seq=source_seq,
                )

            generation = max(1, int(session_row.generation), int(session.generation))
            pipeline_run_id = session_row.pipeline_run_id
            if pipeline_run_id is not None:
                run = await db.get(
                    RecordingPipelineRun,
                    pipeline_run_id,
                    with_for_update=True,
                )
                if (
                    run is None
                    or run.tenant_id != session.tenant_id
                    or run.recording_id != session.recording_id
                    or run.generation != generation
                ):
                    raise RuntimeError("streaming pipeline generation changed")
                if run.state in {
                    "ready",
                    "ready_no_speech",
                    "failed_terminal",
                    "superseded",
                }:
                    raise RuntimeError("streaming pipeline run is no longer writable")
                run.state = "segments"

            max_segment_idx = (
                await db.execute(
                    select(func.max(Segment.idx)).where(
                        Segment.recording_id == session.recording_id,
                        Segment.generation == generation,
                    )
                )
            ).scalar_one_or_none()
            next_segment_idx = int(max_segment_idx) + 1 if max_segment_idx is not None else 0
            segment = Segment(
                tenant_id=session.tenant_id,
                recording_id=session.recording_id,
                pipeline_run_id=pipeline_run_id,
                generation=generation,
                idx=next_segment_idx,
                start_sec=start_sec,
                end_sec=end_sec,
                transcript=text,
                text_scrubbed=scrubbed_text,
                speaker=(
                    str(segment_geometry["speaker"])
                    if segment_geometry.get("speaker") is not None
                    else None
                ),
                vad_conf=(
                    float(segment_geometry["vad_conf"])
                    if segment_geometry.get("vad_conf") is not None
                    else None
                ),
            )
            db.add(segment)
            await db.flush()

            max_chunk_ordinal = (
                await db.execute(
                    select(func.max(Chunk.ordinal)).where(
                        Chunk.recording_id == session.recording_id,
                        Chunk.generation == generation,
                    )
                )
            ).scalar_one_or_none()
            next_chunk_ordinal = int(max_chunk_ordinal) + 1 if max_chunk_ordinal is not None else 0
            content_hash = hashlib.sha256(scrubbed_text.encode("utf-8")).hexdigest()
            chunk = Chunk(
                tenant_id=session.tenant_id,
                recording_id=session.recording_id,
                pipeline_run_id=pipeline_run_id,
                generation=generation,
                ordinal=next_chunk_ordinal,
                segment_ids=[int(segment.id)],
                text=scrubbed_text,
                token_n=max(1, len(scrubbed_text.split())),
                content_hash=content_hash,
            )
            db.add(chunk)
            await db.flush()
            db.add(
                ChunkSegment(
                    tenant_id=session.tenant_id,
                    recording_id=session.recording_id,
                    pipeline_run_id=pipeline_run_id,
                    generation=generation,
                    chunk_id=int(chunk.id),
                    segment_id=int(segment.id),
                    ordinal=0,
                )
            )

            event_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "tenant_id": session.tenant_id,
                        "session_id": session.session_id.value,
                        "recording_id": session.recording_id,
                        "source_event_key": source_event_key,
                        "segment_id": int(segment.id),
                        "chunk_id": int(chunk.id),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for projection_type in _REQUIRED_PROJECTIONS:
                db.add(
                    ProjectionOutbox(
                        tenant_id=session.tenant_id,
                        recording_id=session.recording_id,
                        pipeline_run_id=pipeline_run_id,
                        generation=generation,
                        projection_type=projection_type,
                        aggregate_type="chunk",
                        aggregate_id=str(chunk.id),
                        payload={
                            "chunk_id": int(chunk.id),
                            "segment_id": int(segment.id),
                            "streaming_session_id": int(session_row.id),
                        },
                        idempotency_key=(f"stream:{event_fingerprint[:48]}:{projection_type}"),
                    )
                )
            receipt = StreamingSegmentReceipt(
                tenant_id=session.tenant_id,
                streaming_session_id=int(session_row.id),
                session_key=session.session_id.value,
                source_event_key=source_event_key,
                source_seq=source_seq,
                recording_id=session.recording_id,
                pipeline_run_id=pipeline_run_id,
                generation=generation,
                segment_id=int(segment.id),
                chunk_id=int(chunk.id),
            )
            db.add(receipt)
            await db.execute(
                update(StreamingPCMFrame)
                .where(
                    StreamingPCMFrame.tenant_id == session.tenant_id,
                    StreamingPCMFrame.session_key == session.session_id.value,
                    StreamingPCMFrame.recording_id == session.recording_id,
                    StreamingPCMFrame.source_seq <= source_seq,
                    StreamingPCMFrame.state == "ACCEPTED",
                )
                .values(
                    state="CONSUMED",
                    consumed_segment_id=int(segment.id),
                )
            )
            session_row.durable_segment_high_watermark = int(segment.id)
            session_row.ack_seq_high_watermark = max(
                int(session_row.ack_seq_high_watermark),
                source_seq,
            )
            await db.flush()

            durable = DurableStreamingSegment(
                segment_id=int(segment.id),
                chunk_id=int(chunk.id),
                generation=generation,
                source_seq=source_seq,
            )

        session.generation = durable.generation
        session.durable_segment_high_watermark = max(
            session.durable_segment_high_watermark,
            durable.segment_id,
        )
        return durable


def _finite_float(value: Any, field_name: str) -> float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _lease_deadline(session: StreamSession) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=max(1.0, float(session.lease_ttl_seconds)))


__all__ = [
    "DurableStreamingSegment",
    "StagedPCMFrame",
    "StreamingDurabilityWriter",
]
