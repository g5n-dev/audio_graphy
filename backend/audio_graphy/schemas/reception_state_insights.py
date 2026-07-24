"""Bounded contracts for cross-reception dialogue-state flow insights."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_STATE_TRANSITION_LIMIT = 100
MAX_STATE_TRANSITION_LIMIT = 200
MAX_STATE_STAGES = 64
MAX_STATE_TOP_TRIGGERS = 5
MAX_STATE_SAMPLE_RECEPTIONS = 5


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StateStageInsight(_StrictModel):
    """One bounded graph node aggregated from matching transitions."""

    state: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=0)
    reception_count: int = Field(ge=0)
    incoming_count: int = Field(ge=0)
    outgoing_count: int = Field(ge=0)
    average_confidence: float = Field(ge=0, le=1)


class StateTriggerInsight(_StrictModel):
    """A frequent trigger for one aggregated directed edge."""

    trigger: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=1)


class StateTransitionInsight(_StrictModel):
    """One directed state pair aggregated across matching receptions."""

    from_state: str = Field(min_length=1, max_length=64)
    to_state: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=1)
    average_confidence: float = Field(ge=0, le=1)
    evidence_count: int = Field(ge=0)
    top_triggers: list[StateTriggerInsight] = Field(
        default_factory=list,
        max_length=MAX_STATE_TOP_TRIGGERS,
    )
    sample_reception_ids: list[int] = Field(
        default_factory=list,
        max_length=MAX_STATE_SAMPLE_RECEPTIONS,
    )


class ReceptionStateInsightsResponse(_StrictModel):
    """Tenant-protected, visualization-ready state-flow snapshot."""

    stages: list[StateStageInsight] = Field(
        default_factory=list,
        max_length=MAX_STATE_STAGES,
    )
    transitions: list[StateTransitionInsight] = Field(
        default_factory=list,
        max_length=MAX_STATE_TRANSITION_LIMIT,
    )
    total_receptions: int = Field(ge=0)
    total_transitions: int = Field(ge=0)
    returned_stages: int = Field(ge=0, le=MAX_STATE_STAGES)
    stage_limit: int = Field(default=MAX_STATE_STAGES, ge=1, le=MAX_STATE_STAGES)
    returned_transitions: int = Field(ge=0, le=MAX_STATE_TRANSITION_LIMIT)
    transition_limit: int = Field(ge=1, le=MAX_STATE_TRANSITION_LIMIT)
    truncated: bool
    generated_at: datetime

    @model_validator(mode="after")
    def validate_aggregate_cardinality(self) -> Self:
        if self.returned_stages != len(self.stages):
            raise ValueError("returned_stages must equal the number of stages")
        if self.returned_transitions != len(self.transitions):
            raise ValueError("returned_transitions must equal the number of transitions")
        visible_transition_count = sum(item.count for item in self.transitions)
        if visible_transition_count > self.total_transitions:
            raise ValueError("visible transition counts cannot exceed total_transitions")
        if any(stage.reception_count > self.total_receptions for stage in self.stages):
            raise ValueError("stage reception_count cannot exceed total_receptions")
        return self


__all__ = [
    "DEFAULT_STATE_TRANSITION_LIMIT",
    "MAX_STATE_SAMPLE_RECEPTIONS",
    "MAX_STATE_STAGES",
    "MAX_STATE_TOP_TRIGGERS",
    "MAX_STATE_TRANSITION_LIMIT",
    "ReceptionStateInsightsResponse",
    "StateStageInsight",
    "StateTransitionInsight",
    "StateTriggerInsight",
]
