"""Contracts for the resumable reception automation workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_graphy.schemas.reception_tags import (
    ALL_DIALOGUE_TARGET_LABELS,
    DialogueTargetLabel,
)

ReceptionAutomationStatus = Literal["pending", "running", "failed", "ready"]
ReceptionAutomationStage = Literal["merge", "segmentation", "tagging", "ready"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReceptionAutomationRequest(_StrictModel):
    """Immutable algorithm choices used for a resumable run."""

    segmentation_algorithm: str = Field(
        default="dialogue-hybrid-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    )
    tag_group_key: str = Field(
        default="reception-rules",
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    )
    tag_group_version: str = Field(
        default="rules-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    )
    target_labels: list[DialogueTargetLabel] = Field(
        default_factory=lambda: list(ALL_DIALOGUE_TARGET_LABELS),
        min_length=1,
        max_length=len(ALL_DIALOGUE_TARGET_LABELS),
    )
    tag_priority: int = Field(default=0, ge=-1_000, le=1_000)

    @model_validator(mode="after")
    def validate_unique_targets(self) -> ReceptionAutomationRequest:
        if len(self.target_labels) != len(set(self.target_labels)):
            raise ValueError("target_labels must be unique")
        return self


class ReceptionAutomationResponse(_StrictModel):
    id: int
    reception_id: int
    status: ReceptionAutomationStatus
    stage: ReceptionAutomationStage
    attempt_count: int = Field(ge=0)
    checkpoints: dict[str, Any]
    segmentation_algorithm: str
    tag_group_key: str
    tag_group_version: str
    target_labels: list[DialogueTargetLabel]
    tag_priority: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


__all__ = [
    "ReceptionAutomationRequest",
    "ReceptionAutomationResponse",
    "ReceptionAutomationStage",
    "ReceptionAutomationStatus",
]
