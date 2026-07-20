"""Integration tests for the AuditLog (audit_logs) model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.user import User


@pytest.mark.integration
class TestAuditLogCRUD:
    """CRUD operations for the audit_logs table."""

    def test_create_audit_log(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            action="decrypt",
            target="recording:12345",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        assert log.id is not None

    def test_read_audit_log(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            action="export",
            target="recording:67890",
            before_value={"status": "indexed"},
            after_value={"status": "archived"},
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        result = db_session.scalar(select(AuditLog).where(AuditLog.action == "export"))
        assert result is not None
        assert result.before_value == {"status": "indexed"}
        assert result.after_value == {"status": "archived"}

    def test_update_audit_log(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            action="delete",
            target="recording:111",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        log.action = "soft_delete"
        db_session.commit()

        result = db_session.get(AuditLog, log.id)
        assert result is not None
        assert result.action == "soft_delete"

    def test_delete_audit_log(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            action="delete",
            target="recording:222",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()
        log_id = log.id

        db_session.delete(log)
        db_session.commit()

        assert db_session.get(AuditLog, log_id) is None


@pytest.mark.integration
class TestAuditLogJSON:
    """JSON column tests for the audit_logs table."""

    def test_json_before_after_value(self, db_session: pytest.fixture) -> None:
        before: dict[str, Any] = {"status": "queued", "path": "/old.wav"}
        after: dict[str, Any] = {"status": "indexed", "path": "/new.wav"}
        log = AuditLog(
            tenant_id="default",
            action="reindex",
            target="recording:333",
            before_value=before,
            after_value=after,
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        result = db_session.get(AuditLog, log.id)
        assert result is not None
        assert result.before_value == before
        assert result.after_value == after

    def test_null_before_after(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            action="login",
            target="user:1",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        result = db_session.get(AuditLog, log.id)
        assert result is not None
        assert result.before_value is None
        assert result.after_value is None


@pytest.mark.integration
class TestAuditLogFK:
    """Foreign key tests for the audit_logs table."""

    def test_fk_user(self, db_session: pytest.fixture) -> None:
        user = User(tenant_id="default", name="Auditor", email="auditor@test.com")
        db_session.add(user)
        db_session.flush()

        log = AuditLog(
            tenant_id="default",
            user_id=user.id,
            action="decrypt",
            target="recording:444",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        result = db_session.get(AuditLog, log.id)
        assert result is not None
        assert result.user_id == user.id

    def test_fk_user_invalid(self, db_session: pytest.fixture) -> None:
        log = AuditLog(
            tenant_id="default",
            user_id=999999,
            action="delete",
            target="recording:555",
            occurred_at=datetime.now(UTC),
        )
        db_session.add(log)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()


@pytest.mark.integration
class TestAuditLogMultiTenant:
    """Multi-tenant isolation for the audit_logs table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        log1 = AuditLog(
            tenant_id="ta",
            action="decrypt",
            target="r:1",
            occurred_at=datetime.now(UTC),
        )
        log2 = AuditLog(
            tenant_id="tb",
            action="decrypt",
            target="r:2",
            occurred_at=datetime.now(UTC),
        )
        db_session.add_all([log1, log2])
        db_session.commit()

        a = db_session.scalars(select(AuditLog).where(AuditLog.tenant_id == "ta")).all()
        b = db_session.scalars(select(AuditLog).where(AuditLog.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
