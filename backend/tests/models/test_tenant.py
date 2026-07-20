"""Integration tests for the Tenant (tenants) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.tenant import Tenant


@pytest.mark.integration
class TestTenantCRUD:
    """CRUD operations for the tenants table."""

    def test_create_tenant(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="default", name="Default Tenant")
        db_session.add(tenant)
        db_session.commit()

        assert tenant.id is not None
        assert tenant.created_at is not None
        assert tenant.updated_at is not None

    def test_read_tenant(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="brand_a", name="Brand A", brand="BrandA", region="East")
        db_session.add(tenant)
        db_session.commit()

        result = db_session.scalar(select(Tenant).where(Tenant.code == "brand_a"))
        assert result is not None
        assert result.name == "Brand A"
        assert result.brand == "BrandA"
        assert result.region == "East"

    def test_update_tenant(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="brand_b", name="Brand B")
        db_session.add(tenant)
        db_session.commit()

        tenant.name = "Brand B Updated"
        db_session.commit()

        result = db_session.scalar(select(Tenant).where(Tenant.code == "brand_b"))
        assert result is not None
        assert result.name == "Brand B Updated"

    def test_delete_tenant(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="to_delete", name="Delete Me")
        db_session.add(tenant)
        db_session.commit()
        tenant_id = tenant.id

        db_session.delete(tenant)
        db_session.commit()

        result = db_session.get(Tenant, tenant_id)
        assert result is None


@pytest.mark.integration
class TestTenantConstraints:
    """Constraint validation for the tenants table."""

    def test_unique_code(self, db_session: pytest.fixture) -> None:
        t1 = Tenant(code="dup_code", name="First")
        db_session.add(t1)
        db_session.commit()

        t2 = Tenant(code="dup_code", name="Second")
        db_session.add(t2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_not_null_code(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(name="No Code")  # type: ignore[call-arg]
        db_session.add(tenant)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_not_null_name(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="no_name")  # type: ignore[call-arg]
        db_session.add(tenant)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestTenantSerialization:
    """Serialization tests for the Tenant model."""

    def test_to_dict(self, db_session: pytest.fixture) -> None:
        tenant = Tenant(code="dict_test", name="Dict Test", brand="DT", region="West")
        db_session.add(tenant)
        db_session.commit()

        d = tenant.to_dict()
        assert d["code"] == "dict_test"
        assert d["name"] == "Dict Test"
        assert d["brand"] == "DT"
        assert d["region"] == "West"
        assert "id" in d
        assert "created_at" in d
        assert "updated_at" in d
