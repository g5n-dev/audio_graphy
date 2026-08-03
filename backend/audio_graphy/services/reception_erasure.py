"""Invalidate reception-derived data when an immutable source is erased."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
)
from audio_graphy.models.reception_audio import (
    ReceptionAudioArtifact,
    ReceptionAudioOperation,
    ReceptionTimelineRevision,
)
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagReviewDecision,
    TagReviewTask,
)


async def _invalidate_canonical_tags(
    session: AsyncSession,
    *,
    tenant_id: str,
    reception_id: int,
    dialogue_unit_ids: list[int],
) -> int:
    """Remove canonical current/facts and scrub review rows for one reception."""
    subject_scope = TagAssignmentFact.subject_type == "reception"
    subject_ids = TagAssignmentFact.subject_id == reception_id
    if dialogue_unit_ids:
        subject_scope = or_(
            subject_scope & subject_ids,
            (TagAssignmentFact.subject_type == "dialogue_unit")
            & TagAssignmentFact.subject_id.in_(dialogue_unit_ids),
        )
    else:
        subject_scope = subject_scope & subject_ids

    fact_ids = list(
        (
            await session.execute(
                select(TagAssignmentFact.id).where(
                    TagAssignmentFact.tenant_id == tenant_id,
                    subject_scope,
                )
            )
        ).scalars()
    )

    current_scope: ColumnElement[bool] = (TagAssignmentCurrent.subject_type == "reception") & (
        TagAssignmentCurrent.subject_id == reception_id
    )
    if dialogue_unit_ids:
        current_scope = or_(
            current_scope,
            (TagAssignmentCurrent.subject_type == "dialogue_unit")
            & TagAssignmentCurrent.subject_id.in_(dialogue_unit_ids),
        )
    await session.execute(
        delete(TagAssignmentCurrent).where(
            TagAssignmentCurrent.tenant_id == tenant_id,
            current_scope,
        )
    )
    if fact_ids:
        await session.execute(
            delete(TagAssignmentCurrent).where(
                TagAssignmentCurrent.tenant_id == tenant_id,
                TagAssignmentCurrent.fact_id.in_(fact_ids),
            )
        )

    task_scope = TagReviewTask.reception_id == reception_id
    if fact_ids:
        task_scope = or_(task_scope, TagReviewTask.proposed_fact_id.in_(fact_ids))
    task_ids = list(
        (
            await session.execute(
                select(TagReviewTask.id).where(
                    TagReviewTask.tenant_id == tenant_id,
                    task_scope,
                )
            )
        ).scalars()
    )
    if task_ids:
        await session.execute(
            update(TagReviewDecision)
            .where(
                TagReviewDecision.tenant_id == tenant_id,
                TagReviewDecision.task_id.in_(task_ids),
            )
            .values(
                corrected_value=None,
                note=None,
                evidence_refs=[],
                resulting_fact_id=None,
            )
        )
        await session.execute(
            update(TagReviewTask)
            .where(
                TagReviewTask.tenant_id == tenant_id,
                TagReviewTask.id.in_(task_ids),
            )
            .values(
                reception_id=None,
                proposed_value=None,
                evidence_refs=[],
                proposed_fact_id=None,
                status="skipped",
                claimed_by=None,
                claimed_at=None,
            )
        )

    if not fact_ids:
        return 0

    # Break nullable immutable-lineage references before hard erasure. Facts
    # outside this reception may not retain a pointer into erased evidence.
    await session.execute(
        update(TagAssignmentFact)
        .where(
            TagAssignmentFact.tenant_id == tenant_id,
            TagAssignmentFact.superseded_fact_id.in_(fact_ids),
        )
        .values(superseded_fact_id=None)
    )
    await session.execute(
        update(TagReviewDecision)
        .where(
            TagReviewDecision.tenant_id == tenant_id,
            TagReviewDecision.resulting_fact_id.in_(fact_ids),
        )
        .values(
            corrected_value=None,
            note=None,
            evidence_refs=[],
            resulting_fact_id=None,
        )
    )
    await session.execute(
        update(TagReviewTask)
        .where(
            TagReviewTask.tenant_id == tenant_id,
            TagReviewTask.proposed_fact_id.in_(fact_ids),
        )
        .values(
            proposed_value=None,
            evidence_refs=[],
            proposed_fact_id=None,
            status="skipped",
            claimed_by=None,
            claimed_at=None,
        )
    )
    await session.execute(
        delete(TagAssignmentFact).where(
            TagAssignmentFact.tenant_id == tenant_id,
            TagAssignmentFact.id.in_(fact_ids),
        )
    )
    return len(fact_ids)


async def invalidate_receptions_for_recording(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
    actor: str,
    counts: dict[str, int] | None = None,
) -> list[str]:
    """Remove stale derivations and return physical artifacts to erase.

    Reception mappings are source-of-truth timeline coordinates. Once one
    source disappears, every derived unit, transition and label can point at
    the wrong time or retain erased transcript evidence. The conservative
    privacy-safe action is therefore to clear all derived rows for each
    affected reception and require a fresh segmentation pass over the
    remaining sources.
    """
    result = await session.execute(
        select(Reception)
        .join(
            ReceptionRecording,
            (ReceptionRecording.reception_id == Reception.id)
            & (ReceptionRecording.tenant_id == tenant_id),
        )
        .where(
            Reception.tenant_id == tenant_id,
            ReceptionRecording.recording_id == recording_id,
        )
        .with_for_update()
    )
    receptions = list(result.scalars().unique().all())
    artifact_paths: list[str] = []

    for reception in receptions:
        if reception.merged_audio_path:
            artifact_paths.append(reception.merged_audio_path)

        unit_ids = list(
            (
                await session.execute(
                    select(DialogueUnit.id).where(
                        DialogueUnit.tenant_id == tenant_id,
                        DialogueUnit.reception_id == reception.id,
                    )
                )
            ).scalars()
        )
        canonical_fact_count = await _invalidate_canonical_tags(
            session,
            tenant_id=tenant_id,
            reception_id=reception.id,
            dialogue_unit_ids=unit_ids,
        )

        artifact_result = await session.execute(
            select(ReceptionAudioArtifact.path).where(
                ReceptionAudioArtifact.tenant_id == tenant_id,
                ReceptionAudioArtifact.reception_id == reception.id,
                ReceptionAudioArtifact.state != "DELETED",
            )
        )
        artifact_paths.extend(str(path) for path in artifact_result.scalars() if path)
        reception.active_timeline_revision_id = None

        await session.execute(
            delete(ProvenanceEvent).where(
                ProvenanceEvent.tenant_id == tenant_id,
                or_(
                    ProvenanceEvent.reception_id == reception.id,
                    (
                        (ProvenanceEvent.object_type == "reception")
                        & (ProvenanceEvent.object_ref == str(reception.id))
                    ),
                ),
            )
        )

        scope = (
            DialogueTagAssignment.tenant_id == tenant_id,
            DialogueTagAssignment.reception_id == reception.id,
        )
        await session.execute(delete(DialogueTagAssignment).where(*scope))
        await session.execute(
            delete(DialogueStateTransition).where(
                DialogueStateTransition.tenant_id == tenant_id,
                DialogueStateTransition.reception_id == reception.id,
            )
        )
        await session.execute(
            delete(DialogueUnit).where(
                DialogueUnit.tenant_id == tenant_id,
                DialogueUnit.reception_id == reception.id,
            )
        )
        await session.execute(
            delete(ReceptionRecording).where(
                ReceptionRecording.tenant_id == tenant_id,
                ReceptionRecording.reception_id == reception.id,
                ReceptionRecording.recording_id == recording_id,
            )
        )
        await session.execute(
            delete(ReceptionAudioArtifact).where(
                ReceptionAudioArtifact.tenant_id == tenant_id,
                ReceptionAudioArtifact.reception_id == reception.id,
            )
        )
        await session.execute(
            delete(ReceptionAudioOperation).where(
                ReceptionAudioOperation.tenant_id == tenant_id,
                ReceptionAudioOperation.reception_id == reception.id,
            )
        )
        await session.execute(
            delete(ReceptionTimelineRevision).where(
                ReceptionTimelineRevision.tenant_id == tenant_id,
                ReceptionTimelineRevision.reception_id == reception.id,
            )
        )

        automation_result = await session.execute(
            select(ReceptionAutomationRun)
            .where(
                ReceptionAutomationRun.tenant_id == tenant_id,
                ReceptionAutomationRun.reception_id == reception.id,
            )
            .with_for_update()
        )
        automation_run = automation_result.scalar_one_or_none()
        if automation_run is not None:
            automation_run.status = "pending"
            automation_run.stage = "merge"
            automation_run.checkpoints = {}
            automation_run.lease_token = None
            automation_run.lease_expires_at = None
            automation_run.last_error_code = None
            automation_run.last_error_message = None
            automation_run.finished_at = None

        previous_version = reception.version
        reception.version += 1
        reception.merged_audio_path = None
        if reception.status != "archived":
            reception.status = "needs_review"

        session.add(
            ProvenanceEvent(
                tenant_id=tenant_id,
                reception_id=reception.id,
                object_type="reception",
                object_ref=str(reception.id),
                event_type="deleted",
                actor=actor,
                algorithm_version=None,
                parent_refs=[
                    {
                        "object_type": "recording",
                        "object_ref": str(recording_id),
                    }
                ],
                evidence_refs=[],
                payload={
                    "reason": "source_recording_erased",
                    "previous_version": previous_version,
                    "version": reception.version,
                    "derivatives_cleared": True,
                    "automation_invalidated": automation_run is not None,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        if counts is not None:
            counts["receptions"] = counts.get("receptions", 0) + 1
            counts["dialogue_units"] = counts.get("dialogue_units", 0) + len(unit_ids)
            counts["canonical_tag_facts"] = (
                counts.get("canonical_tag_facts", 0) + canonical_fact_count
            )

    # Preserve deterministic payloads/idempotency without retrying a path twice.
    return list(dict.fromkeys(artifact_paths))


def erase_reception_artifacts(
    artifact_paths: list[str],
    *,
    allowed_root: Path,
) -> list[Path]:
    """Delete only generated reception artifacts below ``assembled_audio``."""
    root = allowed_root.resolve(strict=True)
    deleted: list[Path] = []
    for raw_path in artifact_paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("reception artifact escapes the working directory") from exc
        if not relative.parts or relative.parts[0] != "assembled_audio":
            raise ValueError("reception artifact is outside assembled_audio")
        if resolved.exists():
            if not resolved.is_file():
                raise ValueError("reception artifact is not a regular file")
            resolved.unlink()
            deleted.append(resolved)
    return deleted


__all__ = [
    "erase_reception_artifacts",
    "invalidate_receptions_for_recording",
]
