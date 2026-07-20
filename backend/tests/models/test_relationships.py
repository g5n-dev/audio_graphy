"""Integration tests for ORM relationships between models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.models.vector_chunk import VectorChunk


@pytest.mark.integration
class TestRecordingRelationships:
    """Test one-to-many relationships from Recording."""

    def test_recording_segments(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH001", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        s1 = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        s2 = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=1,
            start_sec=5.0,
            end_sec=10.0,
        )
        db_session.add_all([s1, s2])
        db_session.commit()

        # Reload recording with eager-loaded segments
        result = db_session.scalar(
            select(Recording)
            .where(Recording.id == rec.id)
            .options(selectinload(Recording.segments))
        )
        assert result is not None
        assert len(result.segments) == 2

    def test_recording_chunks(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH002", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        c1 = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="A",
            token_n=5,
            content_hash="rel_c1",
        )
        c2 = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[2],
            text="B",
            token_n=6,
            content_hash="rel_c2",
        )
        db_session.add_all([c1, c2])
        db_session.commit()

        result = db_session.scalar(
            select(Recording).where(Recording.id == rec.id).options(selectinload(Recording.chunks))
        )
        assert result is not None
        assert len(result.chunks) == 2

    def test_recording_tag_facts(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH003", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        tf = TagFact(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="p",
            tag_value="v",
            version=1,
            prompt_version="v1",
            model_version="m1",
            source="llm",
            input_hash="h",
            computed_at=datetime.now(UTC),
        )
        db_session.add(tf)
        db_session.commit()

        result = db_session.scalar(
            select(Recording)
            .where(Recording.id == rec.id)
            .options(selectinload(Recording.tag_facts))
        )
        assert result is not None
        assert len(result.tag_facts) == 1

    def test_recording_current_tags(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH004", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="p",
            tag_value="v",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc)
        db_session.commit()

        result = db_session.scalar(
            select(Recording)
            .where(Recording.id == rec.id)
            .options(selectinload(Recording.current_tags))
        )
        assert result is not None
        assert len(result.current_tags) == 1


@pytest.mark.integration
class TestChildRelationships:
    """Test many-to-one relationships from child to parent."""

    def test_segment_recording(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH005", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add(seg)
        db_session.commit()

        result = db_session.scalar(
            select(Segment).where(Segment.id == seg.id).options(selectinload(Segment.recording))
        )
        assert result is not None
        assert result.recording is not None
        assert result.recording.store_id == "SH005"


@pytest.mark.integration
class TestCascadeDelete:
    """Test cascade delete behavior."""

    def test_delete_recording_cascades_segments(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH006", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add(seg)
        db_session.commit()
        seg_id = seg.id

        db_session.delete(rec)
        db_session.commit()

        assert db_session.get(Segment, seg_id) is None

    def test_delete_chunk_cascades_vectors(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH007", path="/test.wav")
        db_session.add(rec)
        db_session.flush()

        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="x",
            token_n=5,
            content_hash="casc_hash",
        )
        db_session.add(chunk)
        db_session.flush()

        import struct

        vc = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=struct.pack("1024f", *([0.1] * 1024)),
        )
        db_session.add(vc)
        db_session.commit()
        vc_id = vc.id

        db_session.delete(chunk)
        db_session.commit()

        assert db_session.get(VectorChunk, vc_id) is None
