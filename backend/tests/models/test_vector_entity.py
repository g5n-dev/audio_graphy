"""Integration tests for the VectorEntity (vectors_entity) model."""

from __future__ import annotations

import struct

import pytest
from sqlalchemy import select

from audio_graphy.models.vector_entity import VectorEntity


def _make_embedding(dims: int = 1024) -> bytes:
    """Create a dummy float32 embedding as bytes."""
    return struct.pack(f"{dims}f", *([0.1] * dims))


@pytest.mark.integration
class TestVectorEntityCRUD:
    """CRUD operations for the vectors_entity table."""

    def test_create_vector_entity(self, db_session: pytest.fixture) -> None:
        ve = VectorEntity(
            tenant_id="default",
            entity_id="entity_001",
            embedding=_make_embedding(),
        )
        db_session.add(ve)
        db_session.commit()

        assert ve.id is not None

    def test_read_vector_entity(self, db_session: pytest.fixture) -> None:
        emb = _make_embedding()
        ve = VectorEntity(
            tenant_id="default",
            entity_id="entity_002",
            embedding=emb,
        )
        db_session.add(ve)
        db_session.commit()

        result = db_session.scalar(
            select(VectorEntity).where(VectorEntity.entity_id == "entity_002")
        )
        assert result is not None
        assert result.embedding == emb

    def test_update_vector_entity(self, db_session: pytest.fixture) -> None:
        ve = VectorEntity(
            tenant_id="default",
            entity_id="entity_003",
            embedding=_make_embedding(),
        )
        db_session.add(ve)
        db_session.commit()

        new_emb = _make_embedding()
        ve.embedding = new_emb
        db_session.commit()

        result = db_session.get(VectorEntity, ve.id)
        assert result is not None
        assert result.embedding == new_emb

    def test_delete_vector_entity(self, db_session: pytest.fixture) -> None:
        ve = VectorEntity(
            tenant_id="default",
            entity_id="entity_004",
            embedding=_make_embedding(),
        )
        db_session.add(ve)
        db_session.commit()
        ve_id = ve.id

        db_session.delete(ve)
        db_session.commit()

        assert db_session.get(VectorEntity, ve_id) is None


@pytest.mark.integration
class TestVectorEntityLargeBinary:
    """LargeBinary column tests for the vectors_entity table."""

    def test_embedding_roundtrip(self, db_session: pytest.fixture) -> None:
        original = _make_embedding(1024)
        ve = VectorEntity(
            tenant_id="default",
            entity_id="binary_test",
            embedding=original,
        )
        db_session.add(ve)
        db_session.commit()

        result = db_session.scalar(
            select(VectorEntity).where(VectorEntity.entity_id == "binary_test")
        )
        assert result is not None
        assert len(result.embedding) == 4096  # 1024 floats * 4 bytes
        # Verify the data is correct
        floats = struct.unpack("1024f", result.embedding)
        assert all(abs(f - 0.1) < 1e-6 for f in floats)


@pytest.mark.integration
class TestVectorEntityMultiTenant:
    """Multi-tenant isolation for the vectors_entity table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        ve1 = VectorEntity(
            tenant_id="ta",
            entity_id="e1",
            embedding=_make_embedding(),
        )
        ve2 = VectorEntity(
            tenant_id="tb",
            entity_id="e2",
            embedding=_make_embedding(),
        )
        db_session.add_all([ve1, ve2])
        db_session.commit()

        a = db_session.scalars(select(VectorEntity).where(VectorEntity.tenant_id == "ta")).all()
        b = db_session.scalars(select(VectorEntity).where(VectorEntity.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
