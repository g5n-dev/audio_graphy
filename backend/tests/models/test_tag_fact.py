"""Integration tests for the TagFact (tag_facts) model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.models.user import User


def _create_recording(db_session: pytest.fixture, tenant_id: str = "default") -> Recording:
    """Helper: create and flush a Recording."""
    rec = Recording(tenant_id=tenant_id, store_id="SH001", path="/test.wav")
    db_session.add(rec)
    db_session.flush()
    return rec


def _make_tag_fact(
    recording_id: int,
    tenant_id: str = "default",
    tag_path: str = "sales/vehicle_recommended",
    tag_value: str = "yes",
    version: int = 1,
    source: str = "llm",
) -> TagFact:
    """Helper: create a TagFact instance."""
    return TagFact(
        tenant_id=tenant_id,
        recording_id=recording_id,
        tag_path=tag_path,
        tag_value=tag_value,
        version=version,
        prompt_version="v1",
        model_version="qwen3.6-27b",
        source=source,
        input_hash="input_hash_001",
        confidence=0.92,
        computed_at=datetime.now(UTC),
    )


@pytest.mark.integration
class TestTagFactCRUD:
    """CRUD operations for the tag_facts table."""

    def test_create_tag_fact(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id)
        db_session.add(tf)
        db_session.commit()

        assert tf.id is not None
        assert tf.source == "llm"

    def test_read_tag_fact(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id, tag_path="sales/follow_up", tag_value="no")
        db_session.add(tf)
        db_session.commit()

        result = db_session.scalar(select(TagFact).where(TagFact.tag_path == "sales/follow_up"))
        assert result is not None
        assert result.tag_value == "no"

    def test_update_tag_fact(self, db_session: pytest.fixture) -> None:
        """Tag facts are append-only; we insert new versions, not update."""
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id)
        db_session.add(tf)
        db_session.commit()

        # Append-only: we don't update, we insert a new version
        tf2 = _make_tag_fact(rec.id, version=2, tag_value="maybe")
        db_session.add(tf2)
        db_session.commit()

        assert tf2.id != tf.id
        assert tf2.version == 2

    def test_delete_tag_fact(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id)
        db_session.add(tf)
        db_session.commit()
        tf_id = tf.id

        db_session.delete(tf)
        db_session.commit()

        assert db_session.get(TagFact, tf_id) is None


@pytest.mark.integration
class TestTagFactConstraints:
    """Constraint validation for the tag_facts table."""

    def test_fk_recording_invalid(self, db_session: pytest.fixture) -> None:
        tf = _make_tag_fact(999999)
        db_session.add(tf)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_unique_recording_path_version(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf1 = _make_tag_fact(rec.id, version=1)
        db_session.add(tf1)
        db_session.commit()

        tf2 = _make_tag_fact(rec.id, version=1)
        db_session.add(tf2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_source_invalid(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id, source="invalid")
        db_session.add(tf)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_version_positive(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        tf = _make_tag_fact(rec.id, version=0)
        db_session.add(tf)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_fk_computed_by_user(self, db_session: pytest.fixture) -> None:
        rec = _create_recording(db_session)
        user = User(tenant_id="default", name="Inspector", email="insp@test.com", role="inspector")
        db_session.add(user)
        db_session.flush()

        tf = _make_tag_fact(rec.id, source="manual")
        tf.computed_by = user.id
        db_session.add(tf)
        db_session.commit()

        result = db_session.get(TagFact, tf.id)
        assert result is not None
        assert result.computed_by == user.id


@pytest.mark.integration
class TestTagFactMultiTenant:
    """Multi-tenant isolation for the tag_facts table (denormalized tenant_id)."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        r1 = Recording(tenant_id="ta", store_id="S1", path="/a.wav")
        r2 = Recording(tenant_id="tb", store_id="S2", path="/b.wav")
        db_session.add_all([r1, r2])
        db_session.flush()

        tf1 = _make_tag_fact(r1.id, tenant_id="ta")
        tf2 = _make_tag_fact(r2.id, tenant_id="tb")
        db_session.add_all([tf1, tf2])
        db_session.commit()

        a_facts = db_session.scalars(select(TagFact).where(TagFact.tenant_id == "ta")).all()
        b_facts = db_session.scalars(select(TagFact).where(TagFact.tenant_id == "tb")).all()

        assert len(a_facts) == 1
        assert len(b_facts) == 1
