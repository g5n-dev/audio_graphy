"""Transactional invalidation bridge from dialogue edits to tag recomputation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagDeployment,
    TagExtractionJob,
    TagGovernanceAuditEvent,
)


async def invalidate_dialogue_unit_currents_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    dialogue_unit_ids: list[int],
) -> None:
    """Remove canonical pointers while preserving immutable assignment facts."""
    normalized_ids = sorted({int(item) for item in dialogue_unit_ids})
    if not normalized_ids:
        raise ValueError("dialogue_unit_ids must not be empty")
    if any(item <= 0 for item in normalized_ids):
        raise ValueError("dialogue_unit_ids must contain positive identifiers")
    await session.execute(
        delete(TagAssignmentCurrent).where(
            TagAssignmentCurrent.tenant_id == tenant_id,
            TagAssignmentCurrent.subject_type == "dialogue_unit",
            TagAssignmentCurrent.subject_id.in_(normalized_ids),
        )
    )


def _invalidation_key(
    *,
    tenant_id: str,
    reception_id: int,
    dialogue_unit_ids: list[int],
    recompute_dialogue_unit_ids: list[int],
    cause: str,
    reception_version: int,
) -> str:
    identity = (
        f"{tenant_id}\x1f{reception_id}\x1f{reception_version}\x1f"
        f"{cause}\x1f{','.join(str(item) for item in dialogue_unit_ids)}\x1f"
        f"{','.join(str(item) for item in recompute_dialogue_unit_ids)}"
    )
    return f"dialogue-edit:{hashlib.sha256(identity.encode()).hexdigest()}"


async def invalidate_dialogue_units_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    reception_id: int,
    dialogue_unit_ids: list[int],
    recompute_dialogue_unit_ids: list[int] | None = None,
    cause: str,
    reception_version: int,
    actor_user_id: int,
) -> TagExtractionJob:
    """Clear only affected current pointers and enqueue one idempotent job.

    The caller owns the surrounding transaction. Immutable facts are never
    changed or removed, so a failed edit rolls back both the dialogue mutation
    and this invalidation/job creation together.
    """

    normalized_ids = sorted({int(item) for item in dialogue_unit_ids})
    if not normalized_ids:
        raise ValueError("dialogue_unit_ids must not be empty")
    if any(item <= 0 for item in normalized_ids):
        raise ValueError("dialogue_unit_ids must contain positive identifiers")
    normalized_recompute_ids = sorted(
        {
            int(item)
            for item in (
                recompute_dialogue_unit_ids
                if recompute_dialogue_unit_ids is not None
                else normalized_ids
            )
        }
    )
    if not normalized_recompute_ids:
        raise ValueError("recompute_dialogue_unit_ids must not be empty")
    if any(item <= 0 for item in normalized_recompute_ids):
        raise ValueError("recompute_dialogue_unit_ids must contain positive identifiers")
    if not set(normalized_recompute_ids).issubset(normalized_ids):
        raise ValueError("recompute_dialogue_unit_ids must be a subset of dialogue_unit_ids")

    scope = {
        "cause": cause,
        "dialogue_unit_ids": normalized_recompute_ids,
        "invalidated_dialogue_unit_ids": normalized_ids,
        "reception_id": reception_id,
        "reception_version": reception_version,
        "subject_type": "dialogue_unit",
    }
    idempotency_key = _invalidation_key(
        tenant_id=tenant_id,
        reception_id=reception_id,
        dialogue_unit_ids=normalized_ids,
        recompute_dialogue_unit_ids=normalized_recompute_ids,
        cause=cause,
        reception_version=reception_version,
    )
    existing = (
        await session.execute(
            select(TagExtractionJob)
            .where(
                TagExtractionJob.tenant_id == tenant_id,
                TagExtractionJob.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    await invalidate_dialogue_unit_currents_in_session(
        session,
        tenant_id=tenant_id,
        dialogue_unit_ids=normalized_ids,
    )
    if existing is not None:
        if existing.job_type != "recompute" or existing.scope != scope:
            raise RuntimeError("tag invalidation idempotency collision")
        return existing

    tagger_version_id = (
        await session.execute(
            select(TagDeployment.tagger_version_id)
            .where(
                TagDeployment.tenant_id == tenant_id,
                TagDeployment.status == "production",
            )
            .order_by(TagDeployment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    job = TagExtractionJob(
        tenant_id=tenant_id,
        job_type="recompute",
        origin="system",
        status="queued",
        scope=scope,
        tagger_version_id=tagger_version_id,
        idempotency_key=idempotency_key,
        total_items=len(normalized_recompute_ids),
        completed_items=0,
        failed_items=0,
        attempt_count=0,
        max_attempts=3,
        revision=1,
        created_by=actor_user_id,
    )
    session.add(job)
    await session.flush()
    session.add(
        TagGovernanceAuditEvent(
            tenant_id=tenant_id,
            resource_type="tag_job",
            resource_id=job.id,
            action="dialogue_edit_invalidated",
            actor_user_id=actor_user_id,
            payload=scope,
            occurred_at=datetime.now(UTC),
        )
    )
    return job


__all__ = [
    "invalidate_dialogue_unit_currents_in_session",
    "invalidate_dialogue_units_in_session",
]
