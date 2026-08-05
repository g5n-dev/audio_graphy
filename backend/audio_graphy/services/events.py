"""Domain-event emission — one helper, called inside producers' transactions.

Producers today (each inside its own ``session.begin()``):

* ``services/indexing.py`` — recording terminal states
  (``recording.indexed`` / ``recording.ready_no_speech`` / ``recording.failed``)
* ``services/tag_governance.py`` — tag-job terminal states
  (``tag_job.completed`` / ``tag_job.failed``)

The rule for adding one: the insert MUST share the transaction that writes the
state it describes, and the payload carries ids, states and error codes — never
transcript text, file paths, or anything else the PIPL boundary keeps out of
event channels.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.models.domain_event import DomainEvent


def emit_domain_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: int | str,
    payload: dict[str, Any],
) -> None:
    """Append one event to the feed inside the caller's transaction.

    Synchronous on purpose: it only stages an ORM object, so a caller cannot
    accidentally commit it in a separate transaction by awaiting at the wrong
    moment.
    """

    session.add(
        DomainEvent(
            tenant_id=tenant_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=payload,
        )
    )
