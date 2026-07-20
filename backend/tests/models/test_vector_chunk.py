"""Integration tests for the VectorChunk (vectors_chunk) model."""

from __future__ import annotations

import struct

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording
from audio_graphy.models.vector_chunk import VectorChunk


def _make_embedding(dims: int = 1024) -> bytes:
    """Create a dummy float32 embedding as bytes."""
    return struct.pack(f"{dims}f", *([0.2] * dims))


def _create_chunk(db_session: pytest.fixture, tenant_id: str = "default") -> Chunk:
    """Helper: create and flush a Recording + Chunk for FK."""
    rec = Recording(tenant_id=tenant_id, store_id="SH001", path="/test.wav")
    db_session.add(rec)
    db_session.flush()
    chunk = Chunk(
        tenant_id=tenant_id,
        recording_id=rec.id,
        segment_ids=[1],
        text="test",
        token_n=5,
        content_hash="vchash",
    )
    db_session.add(chunk)
    db_session.flush()
    return chunk


@pytest.mark.integration
class TestVectorChunkCRUD:
    """CRUD operations for the vectors_chunk table."""

    def test_create_vector_chunk(self, db_session: pytest.fixture) -> None:
        chunk = _create_chunk(db_session)
        vc = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=_make_embedding(),
        )
        db_session.add(vc)
        db_session.commit()

        assert vc.id is not None

    def test_read_vector_chunk(self, db_session: pytest.fixture) -> None:
        chunk = _create_chunk(db_session)
        emb = _make_embedding()
        vc = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=emb,
        )
        db_session.add(vc)
        db_session.commit()

        result = db_session.scalar(select(VectorChunk).where(VectorChunk.chunk_id == chunk.id))
        assert result is not None
        assert result.embedding == emb

    def test_delete_vector_chunk(self, db_session: pytest.fixture) -> None:
        chunk = _create_chunk(db_session)
        vc = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=_make_embedding(),
        )
        db_session.add(vc)
        db_session.commit()
        vc_id = vc.id

        db_session.delete(vc)
        db_session.commit()

        assert db_session.get(VectorChunk, vc_id) is None


@pytest.mark.integration
class TestVectorChunkConstraints:
    """Constraint validation for the vectors_chunk table."""

    def test_fk_chunk_invalid(self, db_session: pytest.fixture) -> None:
        vc = VectorChunk(
            tenant_id="default",
            chunk_id=999999,
            embedding=_make_embedding(),
        )
        db_session.add(vc)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_unique_tenant_chunk(self, db_session: pytest.fixture) -> None:
        chunk = _create_chunk(db_session)
        vc1 = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=_make_embedding(),
        )
        db_session.add(vc1)
        db_session.commit()

        vc2 = VectorChunk(
            tenant_id="default",
            chunk_id=chunk.id,
            embedding=_make_embedding(),
        )
        db_session.add(vc2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestVectorChunkMultiTenant:
    """Multi-tenant isolation for the vectors_chunk table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        c1 = _create_chunk(db_session, tenant_id="ta")
        c2 = _create_chunk(db_session, tenant_id="tb")

        vc1 = VectorChunk(
            tenant_id="ta",
            chunk_id=c1.id,
            embedding=_make_embedding(),
        )
        vc2 = VectorChunk(
            tenant_id="tb",
            chunk_id=c2.id,
            embedding=_make_embedding(),
        )
        db_session.add_all([vc1, vc2])
        db_session.commit()

        a = db_session.scalars(select(VectorChunk).where(VectorChunk.tenant_id == "ta")).all()
        b = db_session.scalars(select(VectorChunk).where(VectorChunk.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
