"""Integration tests for the Chunk (chunks) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording


def _create_recording(db_session: pytest.fixture, tenant_id: str = "default") -> Recording:
    """Helper: create and flush a Recording for chunk FK."""
    rec = Recording(tenant_id=tenant_id, store_id="SH001", path="/test.wav")
    db_session.add(rec)
    db_session.flush()
    return rec


@pytest.mark.integration
class TestChunkCRUD:
    """CRUD operations for the chunks table."""

    def test_create_chunk(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1, 2, 3],
            text="This is a chunk of text",
            token_n=42,
            content_hash="abc123hash",
        )
        db_session.add(chunk)
        db_session.commit()

        assert chunk.id is not None
        assert chunk.segment_ids == [1, 2, 3]

    def test_read_chunk(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="Read me",
            token_n=5,
            content_hash="readhash",
        )
        db_session.add(chunk)
        db_session.commit()

        result = db_session.scalar(select(Chunk).where(Chunk.content_hash == "readhash"))
        assert result is not None
        assert result.text == "Read me"
        assert result.segment_ids == [1]

    def test_update_chunk(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="Original",
            token_n=5,
            content_hash="uphash",
        )
        db_session.add(chunk)
        db_session.commit()

        chunk.text = "Updated text"
        db_session.commit()

        result = db_session.get(Chunk, chunk.id)
        assert result is not None
        assert result.text == "Updated text"

    def test_delete_chunk(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="Delete",
            token_n=5,
            content_hash="delhash",
        )
        db_session.add(chunk)
        db_session.commit()
        chunk_id = chunk.id

        db_session.delete(chunk)
        db_session.commit()

        assert db_session.get(Chunk, chunk_id) is None


@pytest.mark.integration
class TestChunkConstraints:
    """Constraint validation for the chunks table."""

    def test_fk_recording_invalid(self, db_session: pytest.fixture) -> None:
        chunk = Chunk(
            tenant_id="default",
            recording_id=999999,
            segment_ids=[1],
            text="x",
            token_n=5,
            content_hash="fkhash",
        )
        db_session.add(chunk)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_equal_content_hash_across_recordings_keeps_provenance(
        self, db_session: pytest.fixture
    ) -> None:
        rec = _create_recording(db_session)
        other = _create_recording(db_session)
        c1 = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="A",
            token_n=5,
            content_hash="samehash",
        )
        db_session.add(c1)
        db_session.commit()

        c2 = Chunk(
            tenant_id="default",
            recording_id=other.id,
            segment_ids=[2],
            text="B",
            token_n=6,
            content_hash="samehash",
        )
        db_session.add(c2)
        db_session.commit()

        rows = list(
            db_session.scalars(
                select(Chunk)
                .where(Chunk.content_hash == "samehash")
                .order_by(Chunk.recording_id)
            )
        )
        assert [row.recording_id for row in rows] == [rec.id, other.id]

    def test_check_token_n_positive(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=[1],
            text="x",
            token_n=0,
            content_hash="zerohash",
        )
        db_session.add(chunk)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestChunkJSON:
    """JSON column tests for the chunks table."""

    def test_segment_ids_json_roundtrip(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        ids = [10, 20, 30, 40]
        chunk = Chunk(
            tenant_id="default",
            recording_id=rec.id,
            segment_ids=ids,
            text="JSON test",
            token_n=10,
            content_hash="jsonhash",
        )
        db_session.add(chunk)
        db_session.commit()

        result = db_session.scalar(select(Chunk).where(Chunk.content_hash == "jsonhash"))
        assert result is not None
        assert result.segment_ids == ids
