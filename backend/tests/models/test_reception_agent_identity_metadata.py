"""Schema-level contracts for stable reception ownership and queue indexes."""

from __future__ import annotations

from audio_graphy.models import Reception, Recording


def test_reception_agent_identity_is_nullable_fk_with_fail_closed_indexes() -> None:
    column = Reception.__table__.c.agent_user_id
    assert column.nullable is True
    foreign_keys = list(column.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "users.id"
    assert foreign_keys[0].ondelete == "SET NULL"

    indexes = {
        index.name: tuple(item.name for item in index.columns)
        for index in Reception.__table__.indexes
    }
    assert indexes["ix_receptions_tenant_started_id"] == (
        "tenant_id",
        "started_at",
        "id",
    )
    assert indexes["ix_receptions_tenant_agent_started_id"] == (
        "tenant_id",
        "agent_user_id",
        "started_at",
        "id",
    )


def test_recording_discovery_has_covering_index() -> None:
    indexes = {
        index.name: tuple(item.name for item in index.columns)
        for index in Recording.__table__.indexes
    }
    assert indexes["ix_recordings_tenant_store_status_recorded_id"] == (
        "tenant_id",
        "store_id",
        "status",
        "recorded_at",
        "id",
    )
