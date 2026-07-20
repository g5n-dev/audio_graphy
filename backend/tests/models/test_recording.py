"""Integration tests for the Recording (recordings) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.recording import Recording


@pytest.mark.integration
class TestRecordingCRUD:
    """CRUD operations for the recordings table."""

    def test_create_recording(self, db_session: pytest.fixture) -> None:
        rec = Recording(
            tenant_id="default",
            store_id="SH001",
            path="/data/audio/test.wav",
        )
        db_session.add(rec)
        db_session.commit()

        assert rec.id is not None
        assert rec.status == "queued"  # default
        assert rec.pipeline_state == "pending"  # default

    def test_read_recording(self, db_session: pytest.fixture) -> None:
        rec = Recording(
            tenant_id="default",
            store_id="SH002",
            path="/data/audio/read.wav",
            agent_name="张敏",
            customer_hash="abc123",
        )
        db_session.add(rec)
        db_session.commit()

        result = db_session.scalar(select(Recording).where(Recording.store_id == "SH002"))
        assert result is not None
        assert result.agent_name == "张敏"
        assert result.customer_hash == "abc123"

    def test_update_recording(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH003", path="/data/audio/up.wav")
        db_session.add(rec)
        db_session.commit()

        rec.status = "indexed"
        rec.pipeline_state = "done"
        db_session.commit()

        result = db_session.scalar(select(Recording).where(Recording.store_id == "SH003"))
        assert result is not None
        assert result.status == "indexed"
        assert result.pipeline_state == "done"

    def test_delete_recording(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH004", path="/data/audio/del.wav")
        db_session.add(rec)
        db_session.commit()
        rec_id = rec.id

        db_session.delete(rec)
        db_session.commit()

        result = db_session.get(Recording, rec_id)
        assert result is None


@pytest.mark.integration
class TestRecordingConstraints:
    """Constraint validation for the recordings table."""

    def test_check_status_invalid(self, db_session: pytest.fixture) -> None:
        rec = Recording(
            tenant_id="default",
            store_id="SH005",
            path="/test.wav",
            status="invalid_status",
        )
        db_session.add(rec)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_pipeline_state_invalid(self, db_session: pytest.fixture) -> None:
        rec = Recording(
            tenant_id="default",
            store_id="SH006",
            path="/test.wav",
            pipeline_state="invalid_state",
        )
        db_session.add(rec)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_not_null_path(self, db_session: pytest.fixture) -> None:
        rec = Recording(tenant_id="default", store_id="SH007")  # type: ignore[call-arg]
        db_session.add(rec)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestRecordingMultiTenant:
    """Multi-tenant isolation for the recordings table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        r1 = Recording(tenant_id="ta", store_id="S1", path="/a.wav")
        r2 = Recording(tenant_id="tb", store_id="S2", path="/b.wav")
        db_session.add_all([r1, r2])
        db_session.commit()

        a_recs = db_session.scalars(select(Recording).where(Recording.tenant_id == "ta")).all()
        b_recs = db_session.scalars(select(Recording).where(Recording.tenant_id == "tb")).all()

        assert len(a_recs) == 1
        assert a_recs[0].store_id == "S1"
        assert len(b_recs) == 1
        assert b_recs[0].store_id == "S2"
