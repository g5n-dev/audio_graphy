"""Invalidate reception-derived data when an immutable source is erased."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
)


async def invalidate_receptions_for_recording(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
    actor: str,
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

    return artifact_paths


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
