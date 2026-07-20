"""Integration tests for the TagStat (tag_stats) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.tag_stat import TagStat


@pytest.mark.integration
class TestTagStatCRUD:
    """CRUD operations for the tag_stats table."""

    def test_create_tag_stat(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH001",
            tag_path="sales/vehicle_recommended",
            tag_value="yes",
            tag_count=5,
        )
        db_session.add(ts)
        db_session.commit()

        assert ts.id is not None
        assert ts.tag_count == 5

    def test_read_tag_stat(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH002",
            agent_name="张敏",
            tag_path="sales/follow_up",
            tag_value="no",
            tag_count=3,
        )
        db_session.add(ts)
        db_session.commit()

        result = db_session.scalar(select(TagStat).where(TagStat.store_id == "SH002"))
        assert result is not None
        assert result.agent_name == "张敏"

    def test_update_tag_stat(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH003",
            tag_path="p",
            tag_value="v",
            tag_count=1,
        )
        db_session.add(ts)
        db_session.commit()

        ts.tag_count = 10
        db_session.commit()

        result = db_session.get(TagStat, ts.id)
        assert result is not None
        assert result.tag_count == 10

    def test_delete_tag_stat(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH004",
            tag_path="del",
            tag_value="v",
            tag_count=1,
        )
        db_session.add(ts)
        db_session.commit()
        ts_id = ts.id

        db_session.delete(ts)
        db_session.commit()

        assert db_session.get(TagStat, ts_id) is None


@pytest.mark.integration
class TestTagStatConstraints:
    """Constraint validation for the tag_stats table."""

    def test_unique_dim(self, db_session: pytest.fixture) -> None:
        ts1 = TagStat(
            tenant_id="default",
            store_id="SH001",
            agent_name="A",
            tag_path="p",
            tag_value="v",
            tag_count=1,
        )
        db_session.add(ts1)
        db_session.commit()

        ts2 = TagStat(
            tenant_id="default",
            store_id="SH001",
            agent_name="A",
            tag_path="p",
            tag_value="v",
            tag_count=2,
        )
        db_session.add(ts2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_count_non_negative(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH001",
            tag_path="neg",
            tag_value="v",
            tag_count=-1,
        )
        db_session.add(ts)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_default_count_zero(self, db_session: pytest.fixture) -> None:
        ts = TagStat(
            tenant_id="default",
            store_id="SH001",
            tag_path="def",
            tag_value="v",
        )
        db_session.add(ts)
        db_session.commit()

        assert ts.tag_count == 0


@pytest.mark.integration
class TestTagStatMultiTenant:
    """Multi-tenant isolation for the tag_stats table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        ts1 = TagStat(
            tenant_id="ta",
            store_id="S1",
            tag_path="p",
            tag_value="v",
            tag_count=1,
        )
        ts2 = TagStat(
            tenant_id="tb",
            store_id="S1",
            tag_path="p",
            tag_value="v",
            tag_count=2,
        )
        db_session.add_all([ts1, ts2])
        db_session.commit()

        a = db_session.scalars(select(TagStat).where(TagStat.tenant_id == "ta")).all()
        b = db_session.scalars(select(TagStat).where(TagStat.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
