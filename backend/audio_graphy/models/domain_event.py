"""Append-only domain event feed backing the SSE stream.

One row per state transition worth telling a client about, inserted in the
SAME transaction as the transition itself — the feed cannot claim something
the database did not commit. The auto-increment ``id`` doubles as the stream
cursor: a consumer that reconnects with ``after=<last seen id>`` misses
nothing and re-reads nothing.

Deliberately NOT the callback outbox. ``integration_callbacks`` tracks
per-receiver delivery state (attempts, lease, dead letter); this table has no
delivery state at all, because an SSE consumer tracks its own cursor. Events
here may legitimately duplicate across pipeline retries — a UI that
invalidates a query twice does no harm, so uniqueness is not worth an index.

Payload discipline matches the open API: ids, states and error codes only.
``agent_user_id`` rides along on recording events so the stream can filter
what an agent role is allowed to notice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase, _utcnow


class DomainEvent(TenantScopedBase):
    __tablename__ = "domain_events"

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # The stream's read pattern: everything in my tenant after my cursor.
        Index("ix_domain_events_cursor", "tenant_id", "id"),
    )
