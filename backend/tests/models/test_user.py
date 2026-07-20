"""Integration tests for the User (users) model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.user import User


@pytest.mark.integration
class TestUserCRUD:
    """CRUD operations for the users table."""

    def test_create_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Alice", email="alice@test.com")
        db_session.add(user)
        db_session.commit()

        assert user.id is not None
        assert user.role == "viewer"  # default value

    def test_read_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Bob", email="bob@test.com", role="admin")
        db_session.add(user)
        db_session.commit()

        result = db_session.scalar(select(User).where(User.email == "bob@test.com"))
        assert result is not None
        assert result.name == "Bob"
        assert result.role == "admin"

    def test_update_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Carol", email="carol@test.com")
        db_session.add(user)
        db_session.commit()

        user.role = "inspector"
        db_session.commit()

        result = db_session.scalar(select(User).where(User.email == "carol@test.com"))
        assert result is not None
        assert result.role == "inspector"

    def test_delete_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Dave", email="dave@test.com")
        db_session.add(user)
        db_session.commit()
        user_id = user.id

        db_session.delete(user)
        db_session.commit()

        result = db_session.get(User, user_id)
        assert result is None


@pytest.mark.integration
class TestUserConstraints:
    """Constraint validation for the users table."""

    def test_check_role_invalid(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Hacker", email="hacker@test.com", role="superadmin")
        db_session.add(user)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_unique_email_per_tenant(self, db_session: pytest.fixture) -> None:
        u1 = User(tenant_id="t1", name="A", email="shared@test.com")
        db_session.add(u1)
        db_session.commit()

        u2 = User(tenant_id="t1", name="B", email="shared@test.com")
        db_session.add(u2)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_same_email_different_tenant(self, db_session: pytest.fixture) -> None:
        u1 = User(tenant_id="t1", name="A", email="shared2@test.com")
        db_session.add(u1)
        db_session.commit()

        u2 = User(tenant_id="t2", name="B", email="shared2@test.com")
        db_session.add(u2)
        db_session.commit()  # Should succeed — different tenant

    def test_not_null_name(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", email="noname@test.com")  # type: ignore[call-arg]
        db_session.add(user)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestUserMultiTenant:
    """Multi-tenant isolation for the users table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        u1 = User(tenant_id="tenant_a", name="A", email="a@tenant.com")
        u2 = User(tenant_id="tenant_b", name="B", email="b@tenant.com")
        db_session.add_all([u1, u2])
        db_session.commit()

        a_users = db_session.scalars(select(User).where(User.tenant_id == "tenant_a")).all()
        b_users = db_session.scalars(select(User).where(User.tenant_id == "tenant_b")).all()

        assert len(a_users) == 1
        assert a_users[0].name == "A"
        assert len(b_users) == 1
        assert b_users[0].name == "B"
