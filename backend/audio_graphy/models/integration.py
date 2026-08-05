"""Open-API integration: machine credentials, upload intents, result callbacks.

Three tables, one story: an external system authenticates with an :class:`ApiKey`,
uploads audio through ``/api/v1/open/recordings`` (recorded as an
:class:`IntegrationUpload` so the caller's own reference stays the join key),
and — when the pipeline reaches a terminal state — receives a signed POST whose
durable intent is an :class:`IntegrationCallback` row.

The callback table clones the shape of ``erasure_outbox`` deliberately: the row
is written in the SAME transaction as the terminal status transition, so "the
recording is indexed" and "the caller will be told" cannot diverge across a
crash. Delivery itself is at-least-once; the ``status`` field in the payload is
authoritative and receivers deduplicate on ``event_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase, _utcnow


class ApiKey(TenantScopedBase):
    """A machine credential for the open API.

    Only the SHA-256 of the key is stored; the plaintext (``agk_…``) is shown
    exactly once, at creation. The webhook signing secret is not stored at
    all — it is derived from the master key and this row's id, so it survives
    nowhere the database backup travels to.
    """

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The hash is the lookup key for every authenticated request; it is
        # globally unique because the key material is, and an index that
        # included tenant_id would force the caller to present the tenant
        # out of band.
        Index("ux_api_keys_hash", "key_hash", unique=True),
        Index("ux_api_keys_tenant_name", "tenant_id", "name", unique=True),
    )


class IntegrationUpload(TenantScopedBase):
    """One external upload, keyed by the caller's own reference.

    ``external_ref`` is the idempotency key: re-POSTing the same reference
    returns the recording that already exists instead of ingesting the audio
    twice. ``callback_url`` is captured per upload because one credential may
    serve several downstream systems.
    """

    __tablename__ = "integration_uploads"

    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    api_key_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("api_keys.id", ondelete="RESTRICT"),
        nullable=False,
    )
    callback_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("ux_integration_uploads_ref", "tenant_id", "external_ref", unique=True),
        Index("ix_integration_uploads_recording", "recording_id"),
    )


class IntegrationCallback(TenantScopedBase):
    """Durable intent to deliver one terminal-state notification.

    Same five-state machine as ``erasure_outbox``; ``available_at`` implements
    the retry backoff and ``dead_letter`` is where an unreachable receiver
    ends up — visible, not silently dropped.
    """

    __tablename__ = "integration_callbacks"

    upload_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_uploads.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    callback_url: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_integration_callbacks_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_integration_callbacks_attempts"),
        Index("ux_integration_callbacks_event", "event_id", unique=True),
        Index("ix_integration_callbacks_claim", "status", "available_at"),
    )
