"""Transactional projection of canonical stage facts onto reception state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.errors import ConflictError, NotFoundError, ValidationError
from audio_graphy.models.reception import DialogueStateTransition, DialogueUnit

MAX_STAGE_TRANSITION_AUDIT_ITEMS = 256


def _transition_snapshot(item: DialogueStateTransition) -> dict[str, Any]:
    return {
        "id": item.id,
        "dialogue_unit_id": item.dialogue_unit_id,
        "sequence_no": item.sequence_no,
        "from_state": item.from_state,
        "to_state": item.to_state,
        "trigger": item.trigger,
        "confidence": item.confidence,
        "evidence_refs": deepcopy(item.evidence_refs),
        "algorithm_version": item.algorithm_version,
    }


async def project_stage_change_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    reception_id: int,
    dialogue_unit_id: int,
    stage: str,
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Update one unit and its state-chain edges without opening a transaction."""

    if len(stage) > 64:
        raise ValidationError(
            "Stage labels must not exceed 64 characters",
            code="TAG_STAGE_VALUE_TOO_LONG",
            detail={"max_length": 64},
        )
    units = list(
        (
            await session.execute(
                select(DialogueUnit)
                .where(
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception_id,
                )
                .order_by(
                    DialogueUnit.unit_index,
                    DialogueUnit.start_sec,
                    DialogueUnit.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    unit = next((item for item in units if item.id == dialogue_unit_id), None)
    if unit is None:
        raise NotFoundError(
            "Dialogue unit not found",
            code="DIALOGUE_UNIT_NOT_FOUND",
        )
    if unit.business_stage == stage:
        return None
    if unit.edit_status == "locked":
        raise ConflictError(
            "Locked dialogue units cannot change stage",
            code="DIALOGUE_UNIT_LOCKED",
            detail={"dialogue_unit_id": unit.id},
        )

    transitions = list(
        (
            await session.execute(
                select(DialogueStateTransition)
                .where(
                    DialogueStateTransition.tenant_id == tenant_id,
                    DialogueStateTransition.reception_id == reception_id,
                )
                .order_by(
                    DialogueStateTransition.sequence_no,
                    DialogueStateTransition.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    transition_snapshots = [_transition_snapshot(item) for item in transitions]
    before_unit = {
        "business_stage": unit.business_stage,
        "version": unit.version,
        "edit_status": unit.edit_status,
    }
    healthy_chain = len(transitions) == len(units) and all(
        transition.dialogue_unit_id == chain_unit.id
        and transition.to_state == (chain_unit.business_stage or "__unknown__")
        and transition.from_state
        == ("__start__" if index == 0 else transitions[index - 1].to_state)
        for index, (chain_unit, transition) in enumerate(zip(units, transitions, strict=True))
    )
    unit.business_stage = stage
    unit.version += 1
    unit.edit_status = "manual_edited"

    rebuilt = not healthy_chain
    if healthy_chain:
        position = next(index for index, item in enumerate(units) if item.id == unit.id)
        affected_positions = [position]
        if position + 1 < len(transitions):
            affected_positions.append(position + 1)
        before_transitions = [transition_snapshots[index] for index in affected_positions]
        target = transitions[position]
        target.to_state = stage
        target.trigger = "manual_tag_correction"
        target.confidence = 1.0
        target.evidence_refs = deepcopy(evidence_refs)
        target.algorithm_version = "manual-tag-edit-v1"
        if position + 1 < len(transitions):
            transitions[position + 1].from_state = stage
        after_transitions = [
            _transition_snapshot(transitions[index]) for index in affected_positions
        ]
    else:
        before_transitions = transition_snapshots[:MAX_STAGE_TRANSITION_AUDIT_ITEMS]
        await session.execute(
            delete(DialogueStateTransition).where(
                DialogueStateTransition.tenant_id == tenant_id,
                DialogueStateTransition.reception_id == reception_id,
            )
        )
        previous_state = "__start__"
        rebuilt_transitions: list[DialogueStateTransition] = []
        for sequence_no, chain_unit in enumerate(units):
            is_target = chain_unit.id == unit.id
            to_state = chain_unit.business_stage or "__unknown__"
            confidence = (
                1.0
                if is_target
                else min(
                    1.0,
                    max(0.0, chain_unit.boundary_confidence or 1.0),
                )
            )
            rebuilt_transitions.append(
                DialogueStateTransition(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    dialogue_unit_id=chain_unit.id,
                    sequence_no=sequence_no,
                    from_state=previous_state,
                    to_state=to_state,
                    trigger=("manual_tag_correction" if is_target else "stage_projection_repair"),
                    confidence=confidence,
                    evidence_refs=deepcopy(evidence_refs if is_target else chain_unit.segment_refs),
                    algorithm_version="manual-tag-edit-v1",
                )
            )
            previous_state = to_state
        session.add_all(rebuilt_transitions)
        await session.flush()
        after_transitions = [
            _transition_snapshot(item)
            for item in rebuilt_transitions[:MAX_STAGE_TRANSITION_AUDIT_ITEMS]
        ]

    return {
        "dialogue_unit_id": unit.id,
        "before": before_unit,
        "after": {
            "business_stage": unit.business_stage,
            "version": unit.version,
            "edit_status": unit.edit_status,
        },
        "state_transitions_before": before_transitions,
        "state_transitions_after": after_transitions,
        "state_transition_count": len(units),
        "state_transition_audit_truncated": (
            rebuilt and max(len(transitions), len(units)) > MAX_STAGE_TRANSITION_AUDIT_ITEMS
        ),
        "state_chain_rebuilt": rebuilt,
    }


__all__ = [
    "MAX_STAGE_TRANSITION_AUDIT_ITEMS",
    "project_stage_change_in_session",
]
