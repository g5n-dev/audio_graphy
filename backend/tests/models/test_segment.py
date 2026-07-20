"""Integration tests for the Segment (segments) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment


def _create_recording(db_session: pytest.fixture, tenant_id: str = "default") -> Recording:
    """Helper: create and flush a Recording for segment FK."""
    rec = Recording(tenant_id=tenant_id, store_id="SH001", path="/test.wav")
    db_session.add(rec)
    db_session.flush()
    return rec


@pytest.mark.integration
class TestSegmentCRUD:
    """CRUD operations for the segments table."""

    def test_create_segment(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.5,
            transcript="Hello world",
            speaker="坐席",
            vad_conf=0.95,
        )
        db_session.add(seg)
        db_session.commit()

        assert seg.id is not None
        assert seg.transcript == "Hello world"

    def test_read_segment(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=1,
            start_sec=5.5,
            end_sec=10.0,
        )
        db_session.add(seg)
        db_session.commit()

        result = db_session.scalar(select(Segment).where(Segment.recording_id == rec.id))
        assert result is not None
        assert result.idx == 1

    def test_update_segment(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add(seg)
        db_session.commit()

        seg.transcript = "Updated transcript"
        db_session.commit()

        result = db_session.get(Segment, seg.id)
        assert result is not None
        assert result.transcript == "Updated transcript"

    def test_delete_segment(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
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

        db_session.delete(seg)
        db_session.commit()

        assert db_session.get(Segment, seg_id) is None


@pytest.mark.integration
class TestSegmentConstraints:
    """Constraint validation for the segments table."""

    def test_fk_recording_invalid(self, db_session: pytest.fixture) -> None:
        seg = Segment(
            tenant_id="default",
            recording_id=999999,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add(seg)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_unique_recording_idx(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        s1 = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add(s1)
        db_session.commit()

        s2 = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=5.0,
            end_sec=10.0,
        )
        db_session.add(s2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_time_order(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        seg = Segment(
            tenant_id="default",
            recording_id=rec.id,
            idx=0,
            start_sec=10.0,
            end_sec=5.0,  # end < start
        )
        db_session.add(seg)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestSegmentMultiTenant:
    """Multi-tenant isolation for the segments table (denormalized tenant_id)."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        r1 = Recording(tenant_id="ta", store_id="S1", path="/a.wav")
        r2 = Recording(tenant_id="tb", store_id="S2", path="/b.wav")
        db_session.add_all([r1, r2])
        db_session.flush()

        s1 = Segment(
            tenant_id="ta",
            recording_id=r1.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        s2 = Segment(
            tenant_id="tb",
            recording_id=r2.id,
            idx=0,
            start_sec=0.0,
            end_sec=5.0,
        )
        db_session.add_all([s1, s2])
        db_session.commit()

        a_segs = db_session.scalars(select(Segment).where(Segment.tenant_id == "ta")).all()
        b_segs = db_session.scalars(select(Segment).where(Segment.tenant_id == "tb")).all()

        assert len(a_segs) == 1
        assert len(b_segs) == 1
