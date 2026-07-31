from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
from audio_graphy.adapters.protocols import StreamSessionId
from audio_graphy.api.ws_stream import _reserve_session_row
from audio_graphy.core.stream_session import StreamSession, hash_consent_token
from audio_graphy.models.chunk import Chunk, ChunkSegment
from audio_graphy.models.pipeline import ProjectionOutbox, RecordingPipelineRun
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.streaming_pcm_frame import StreamingPCMFrame
from audio_graphy.models.streaming_segment_receipt import StreamingSegmentReceipt
from audio_graphy.models.streaming_session import StreamingSession as StreamingSessionORM
from audio_graphy.services.streaming_durability import StreamingDurabilityWriter


async def _seed_stream(
    session_factory: Any,
) -> tuple[StreamingSessionORM, RecordingPipelineRun]:
    now = datetime.now(UTC)
    async with session_factory() as db, db.begin():
        recording = Recording(
            tenant_id="tenant-a",
            store_id="store-a",
            path="/tmp/live.wav",
            status="processing",
            pipeline_state="asr",
        )
        db.add(recording)
        await db.flush()
        run = RecordingPipelineRun(
            tenant_id="tenant-a",
            recording_id=recording.id,
            generation=1,
            idempotency_key="stream:durable-session",
            source_fingerprint="a" * 64,
            config_fingerprint="b" * 64,
            state="asr",
            attempt_count=1,
            required_projections=["vector", "graph", "file_index", "tag"],
            completed_projections=[],
            started_at=now,
        )
        db.add(run)
        await db.flush()
        row = StreamingSessionORM(
            tenant_id="tenant-a",
            session_id="durable-session",
            epoch=1,
            status="ACTIVE",
            generation=1,
            pipeline_run_id=run.id,
            ack_seq_high_watermark=-1,
            durable_segment_high_watermark=0,
            lease_token="lease-epoch-1",
            recording_id=recording.id,
            user_id=7,
            started_at=now,
            consent_token_hash=hash_consent_token("consent"),
        )
        db.add(row)
        await db.flush()
        row_id = int(row.id)
        run_id = int(run.id)

    async with session_factory() as db:
        persisted_row = await db.get(StreamingSessionORM, row_id)
        persisted_run = await db.get(RecordingPipelineRun, run_id)
        assert persisted_row is not None
        assert persisted_run is not None
        return persisted_row, persisted_run


def _core_session(
    row: StreamingSessionORM,
    run: RecordingPipelineRun,
) -> StreamSession:
    return StreamSession(
        session_id=StreamSessionId(value=row.session_id),
        tenant_id=str(row.tenant_id),
        recording_id=int(row.recording_id),
        user_id=row.user_id,
        consent_token_hash=row.consent_token_hash,
        vad_adapter=MockStreamingVADAdapter(latency_ms=0),
        asr_adapter=MockStreamingASRAdapter(
            connect_latency_ms=0,
            push_latency_ms=0,
        ),
        epoch=int(row.epoch),
        generation=int(row.generation),
        pipeline_run_id=int(run.id),
        lease_token=row.lease_token,
        persistence_id=int(row.id),
    )


@pytest.mark.asyncio
async def test_confirmed_event_is_one_transactional_segment_chunk_outbox_chain(
    session_factory: Any,
) -> None:
    row, run = await _seed_stream(session_factory)
    session = _core_session(row, run)
    writer = StreamingDurabilityWriter(session_factory)
    pcm = b"\x01\x00" * 512

    staged = await writer.stage_frame(session, source_seq=8, pcm=pcm)
    assert staged.duplicate is False
    duplicate = await writer.stage_frame(session, source_seq=8, pcm=pcm)
    assert duplicate.duplicate is True
    with pytest.raises(
        RuntimeError,
        match="different audio",
    ):
        await writer.stage_frame(session, source_seq=8, pcm=b"\x02\x00" * 512)

    event = {
        "type": "segment_confirmed",
        "session_id": "durable-session",
        "seq": 8,
        "sentence_id": 3,
        "text": "联系电话 13800138000",
        "segment": {
            "idx": 0,
            "start_sec": 0.0,
            "end_sec": 1.0,
            "speaker": "customer",
            "vad_conf": 0.99,
            "transcript": "联系电话 13800138000",
        },
        "durable": False,
    }
    durable = await writer.persist_confirmed(session, event)
    replay = await writer.persist_confirmed(session, event)
    assert replay == durable
    assert durable.generation == 1

    async with session_factory() as db:
        segment = await db.get(Segment, durable.segment_id)
        chunk = await db.get(Chunk, durable.chunk_id)
        assert segment is not None
        assert chunk is not None
        assert segment.pipeline_run_id == run.id
        assert segment.generation == 1
        assert segment.transcript == "联系电话 13800138000"
        assert "13800138000" not in str(segment.text_scrubbed)
        assert chunk.segment_ids == [segment.id]
        assert "13800138000" not in chunk.text
        assert (
            await db.execute(
                select(func.count(ChunkSegment.id)).where(
                    ChunkSegment.chunk_id == chunk.id,
                    ChunkSegment.segment_id == segment.id,
                )
            )
        ).scalar_one() == 1
        outboxes = list(
            (
                await db.execute(
                    select(ProjectionOutbox).where(
                        ProjectionOutbox.recording_id == row.recording_id,
                        ProjectionOutbox.generation == 1,
                    )
                )
            ).scalars()
        )
        assert {outbox.projection_type for outbox in outboxes} == {
            "vector",
            "graph",
            "file_index",
            "tag",
        }
        assert len(outboxes) == 4
        receipt_count = (
            await db.execute(select(func.count(StreamingSegmentReceipt.id)))
        ).scalar_one()
        assert receipt_count == 1
        frame = (
            await db.execute(
                select(StreamingPCMFrame).where(
                    StreamingPCMFrame.session_key == "durable-session",
                    StreamingPCMFrame.source_seq == 8,
                )
            )
        ).scalar_one()
        assert frame.state == "CONSUMED"
        assert frame.consumed_segment_id == segment.id

    assert session.durable_segment_high_watermark == durable.segment_id


@pytest.mark.asyncio
async def test_pending_frame_survives_reconnect_and_consumed_replay_is_safe(
    session_factory: Any,
) -> None:
    row, run = await _seed_stream(session_factory)
    first = _core_session(row, run)
    writer = StreamingDurabilityWriter(session_factory)
    pcm = b"\x03\x00" * 512
    await writer.stage_frame(first, source_seq=12, pcm=pcm)

    async with session_factory() as db, db.begin():
        current_row = await db.get(
            StreamingSessionORM,
            row.id,
            with_for_update=True,
        )
        assert current_row is not None
        current_row.status = "INCOMPLETE"
        current_row.lease_token = None
        reconnect_row = StreamingSessionORM(
            tenant_id="tenant-a",
            session_id="durable-session",
            epoch=2,
            status="ACTIVE",
            generation=1,
            pipeline_run_id=run.id,
            ack_seq_high_watermark=12,
            durable_segment_high_watermark=0,
            lease_token="lease-epoch-2",
            recording_id=row.recording_id,
            user_id=7,
            started_at=datetime.now(UTC),
            consent_token_hash=hash_consent_token("consent"),
        )
        db.add(reconnect_row)
        await db.flush()
        reconnect_id = int(reconnect_row.id)
    async with session_factory() as db:
        reconnect_row = await db.get(StreamingSessionORM, reconnect_id)
        assert reconnect_row is not None
    reconnect = _core_session(reconnect_row, run)

    pending = await writer.pending_frames(reconnect)
    assert [(item.source_seq, item.pcm) for item in pending] == [(12, pcm)]
    event = {
        "type": "segment_confirmed",
        "seq": 12,
        "sentence_id": 5,
        "text": "重连后仍然一致",
        "segment": {
            "start_sec": 0.0,
            "end_sec": 0.5,
            "transcript": "重连后仍然一致",
        },
    }
    first_durable = await writer.persist_confirmed(reconnect, event)
    assert await writer.pending_frames(reconnect) == []
    consumed = await writer.stage_frame(reconnect, source_seq=12, pcm=pcm)
    assert consumed.state == "CONSUMED"

    third = _core_session(reconnect_row, run)
    replay = await writer.persist_confirmed(third, event)
    assert replay.segment_id == first_durable.segment_id
    assert replay.chunk_id == first_durable.chunk_id


@pytest.mark.asyncio
async def test_revoked_epoch_cannot_ack_or_publish_after_reconnect(
    session_factory: Any,
) -> None:
    row, run = await _seed_stream(session_factory)
    stale = _core_session(row, run)
    writer = StreamingDurabilityWriter(session_factory)

    async with session_factory() as db, db.begin():
        persisted = await db.get(
            StreamingSessionORM,
            row.id,
            with_for_update=True,
        )
        assert persisted is not None
        persisted.status = "INCOMPLETE"
        persisted.lease_token = None

    with pytest.raises(RuntimeError, match="lease was lost"):
        await writer.stage_frame(stale, source_seq=1, pcm=b"\x01\x00" * 512)
    with pytest.raises(RuntimeError, match="lease was lost"):
        await writer.persist_confirmed(
            stale,
            {
                "type": "segment_confirmed",
                "seq": 1,
                "sentence_id": 1,
                "text": "旧连接迟到",
                "segment": {
                    "start_sec": 0.0,
                    "end_sec": 0.5,
                    "transcript": "旧连接迟到",
                },
            },
        )


@pytest.mark.asyncio
async def test_session_epoch_reservation_requires_explicit_bounded_resume(
    session_factory: Any,
) -> None:
    async with session_factory() as db, db.begin():
        recording = Recording(
            tenant_id="tenant-a",
            store_id="store-a",
            path="/tmp/resume.wav",
            status="processing",
        )
        db.add(recording)
        await db.flush()
        recording_id = int(recording.id)
    app = SimpleNamespace(
        state=SimpleNamespace(session_factory=session_factory),
    )

    first = await _reserve_session_row(
        app,
        tenant_id="tenant-a",
        session_id="bounded-resume",
        recording_id=recording_id,
        user_id=7,
        consent_token_hash=hash_consent_token("consent"),
        timeout_sec=30,
        resume_from_seq=0,
        resume_requested=False,
        resume_token=None,
    )
    assert first[1] == 1
    assert first[4]

    with pytest.raises(ValueError, match="resume is required"):
        await _reserve_session_row(
            app,
            tenant_id="tenant-a",
            session_id="bounded-resume",
            recording_id=recording_id,
            user_id=7,
            consent_token_hash=hash_consent_token("consent"),
            timeout_sec=30,
            resume_from_seq=0,
            resume_requested=False,
            resume_token=None,
        )
    with pytest.raises(ValueError, match="cannot be preempted"):
        await _reserve_session_row(
            app,
            tenant_id="tenant-a",
            session_id="bounded-resume",
            recording_id=recording_id,
            user_id=7,
            consent_token_hash=hash_consent_token("consent"),
            timeout_sec=30,
            resume_from_seq=0,
            resume_requested=True,
            resume_token="wrong-lease",
        )
    with pytest.raises(ValueError, match="ahead of the durable watermark"):
        await _reserve_session_row(
            app,
            tenant_id="tenant-a",
            session_id="bounded-resume",
            recording_id=recording_id,
            user_id=7,
            consent_token_hash=hash_consent_token("consent"),
            timeout_sec=30,
            resume_from_seq=1,
            resume_requested=True,
            resume_token=first[4],
        )

    resumed = await _reserve_session_row(
        app,
        tenant_id="tenant-a",
        session_id="bounded-resume",
        recording_id=recording_id,
        user_id=7,
        consent_token_hash=hash_consent_token("consent"),
        timeout_sec=30,
        resume_from_seq=0,
        resume_requested=True,
        resume_token=first[4],
    )
    assert resumed[1] == 2
    assert resumed[2] == first[2]
    assert resumed[3] == first[3]
    assert resumed[4] and resumed[4] != first[4]

    async with session_factory() as db:
        sessions = list(
            (
                await db.execute(
                    select(StreamingSessionORM)
                    .where(
                        StreamingSessionORM.tenant_id == "tenant-a",
                        StreamingSessionORM.session_id == "bounded-resume",
                    )
                    .order_by(StreamingSessionORM.epoch)
                )
            ).scalars()
        )
    assert [row.status for row in sessions] == ["INCOMPLETE", "RESERVING"]
    assert sessions[0].lease_token is None
    assert sessions[1].lease_token == resumed[4]
