"""Integration tests for the TagCurrent (tag_current) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_current import TagCurrent


def _create_recording(db_session: pytest.fixture, tenant_id: str = "default") -> Recording:
    rec = Recording(tenant_id=tenant_id, store_id="SH001", path="/test.wav")
    db_session.add(rec)
    db_session.flush()
    return rec


@pytest.mark.integration
class TestTagCurrentCRUD:
    """CRUD operations for the tag_current table."""

    def test_create_tag_current(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="sales/vehicle_recommended",
            tag_value="yes",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc)
        db_session.commit()

        assert tc.id is not None

    def test_read_tag_current(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="sales/follow_up",
            tag_value="no",
            version=2,
            prompt_version="v2",
        )
        db_session.add(tc)
        db_session.commit()

        result = db_session.scalar(
            select(TagCurrent).where(TagCurrent.tag_path == "sales/follow_up")
        )
        assert result is not None
        assert result.version == 2

    def test_update_tag_current(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="sales/x",
            tag_value="yes",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc)
        db_session.commit()

        tc.tag_value = "no"
        tc.version = 2
        db_session.commit()

        result = db_session.get(TagCurrent, tc.id)
        assert result is not None
        assert result.tag_value == "no"

    def test_delete_tag_current(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="sales/del",
            tag_value="yes",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc)
        db_session.commit()
        tc_id = tc.id

        db_session.delete(tc)
        db_session.commit()

        assert db_session.get(TagCurrent, tc_id) is None


@pytest.mark.integration
class TestTagCurrentConstraints:
    """Constraint validation for the tag_current table."""

    def test_fk_recording_invalid(self, db_session: pytest.fixture) -> None:
        tc = TagCurrent(
            tenant_id="default",
            recording_id=999999,
            tag_path="x",
            tag_value="y",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_unique_recording_path(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc1 = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="dup",
            tag_value="a",
            version=1,
            prompt_version="v1",
        )
        db_session.add(tc1)
        db_session.commit()

        tc2 = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="dup",
            tag_value="b",
            version=2,
            prompt_version="v2",
        )
        db_session.add(tc2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_version_positive(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tc = TagCurrent(
            tenant_id="default",
            recording_id=rec.id,
            tag_path="zv",
            tag_value="y",
            version=0,
            prompt_version="v1",
        )
        db_session.add(tc)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestTagCurrentMultiTenant:
    """Multi-tenant isolation for the tag_current table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        r1 = Recording(tenant_id="ta", store_id="S1", path="/a.wav")
        r2 = Recording(tenant_id="tb", store_id="S2", path="/b.wav")
        db_session.add_all([r1, r2])
        db_session.flush()

        tc1 = TagCurrent(
            tenant_id="ta",
            recording_id=r1.id,
            tag_path="p",
            tag_value="v1",
            version=1,
            prompt_version="v1",
        )
        tc2 = TagCurrent(
            tenant_id="tb",
            recording_id=r2.id,
            tag_path="p",
            tag_value="v2",
            version=1,
            prompt_version="v1",
        )
        db_session.add_all([tc1, tc2])
        db_session.commit()

        a = db_session.scalars(select(TagCurrent).where(TagCurrent.tenant_id == "ta")).all()
        b = db_session.scalars(select(TagCurrent).where(TagCurrent.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
