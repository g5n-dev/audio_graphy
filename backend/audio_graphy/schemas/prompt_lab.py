"""Public contracts for the prompt-lab API.

``redaction_mode`` deliberately omits ``verbatim``. The value exists in the model
layer for tests and debugging, but a prompt served to a provider must never carry
unredacted customer speech, so the API offers no way to ask for it.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from audio_graphy.schemas.tag_governance import StrictModel

CompilerName = Literal[
    "builtin",
    "builtin_grounded",
    "dspy_mipro",
    "dspy_bootstrap",
    "dspy_gepa",
    "textgrad_tgd",
]
EfficiencyPolicy = Literal["token_reduction_v1", "quality_uplift_v1"]


class PromptCompilerConfig(StrictModel):
    compiler: CompilerName = "builtin"
    max_patches: int = Field(default=8, ge=1, le=32)
    min_cluster_support: int = Field(default=3, ge=1, le=100)
    instruction_candidates: int = Field(default=4, ge=1, le=8)
    textgrad_iterations: int = Field(default=2, ge=1, le=4)
    demo_count: Literal[0, 2, 4] = 0
    # verbatim is intentionally absent -- see the module docstring.
    redaction_mode: Literal["synthetic", "masked"] = "synthetic"
    max_prompt_tokens: int = Field(default=3_072, ge=512, le=8_192)
    efficiency_policy: EfficiencyPolicy = "quality_uplift_v1"
    seed: int = Field(default=0, ge=0, le=2**31 - 1)


class PromptCompileBudget(StrictModel):
    max_provider_calls: int = Field(default=120, ge=1, le=1_000)
    max_provider_tokens: int = Field(default=1_500_000, ge=1_000, le=50_000_000)
    max_cost_microunits: int = Field(default=2_000_000, ge=1)
    max_wall_seconds: int = Field(default=1_800, ge=60, le=7_200)


class PromptCompilationCreate(StrictModel):
    baseline_tagger_version_id: int = Field(gt=0)
    gold_set_version_id: int | None = Field(default=None, gt=0)
    compiler: PromptCompilerConfig = Field(default_factory=PromptCompilerConfig)
    budget: PromptCompileBudget = Field(default_factory=PromptCompileBudget)


class PatchDecisionItem(StrictModel):
    patch_id: str = Field(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=1_000)


class PatchDecisionBatch(StrictModel):
    decisions: list[PatchDecisionItem] = Field(min_length=1, max_length=128)
    dropped_demo_ids: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _reject_duplicate_patches(self) -> Self:
        seen = {item.patch_id for item in self.decisions}
        if len(seen) != len(self.decisions):
            raise ValueError("each patch_id may appear at most once")
        if len(set(self.dropped_demo_ids)) != len(self.dropped_demo_ids):
            raise ValueError("each demo_id may appear at most once")
        return self


class PromoteArtifactCreate(StrictModel):
    version_suffix: str = Field(min_length=1, max_length=32, pattern=r"^[\w.-]+$")
    change_summary: str = Field(min_length=8, max_length=4_000)
    efficiency_policy: EfficiencyPolicy = "quality_uplift_v1"


def artifact_resource(row: Any, *, include_prompt: bool = True) -> dict[str, Any]:
    """Shape an artifact row for the API, keeping the wire contract explicit."""

    payload: dict[str, Any] = {
        "id": int(row.id),
        "compilation_id": int(row.compilation_id),
        "optimization_run_id": row.optimization_run_id,
        "baseline_tagger_version_id": int(row.baseline_tagger_version_id),
        "gold_set_version_id": row.gold_set_version_id,
        "parent_artifact_id": row.parent_artifact_id,
        "candidate_tagger_version_id": row.candidate_tagger_version_id,
        "compiler": str(row.compiler),
        "compiler_version": str(row.compiler_version),
        "metric_version": str(row.metric_version),
        "status": str(row.status),
        "prompt_token_estimate": int(row.prompt_token_estimate),
        "accepted_patch_ids": list(row.accepted_patch_ids or []),
        "input_budget_report": dict(row.input_budget_report or {}),
        "redaction_report": dict(row.redaction_report or {}),
        "artifact_checksum": str(row.artifact_checksum),
        "created_at": row.created_at,
    }
    if include_prompt:
        payload["baseline_prompt"] = str(row.baseline_prompt)
        payload["rendered_prompt"] = str(row.rendered_prompt)
        payload["patches"] = list(row.patches or [])
        payload["demos"] = list(row.demos or [])
    return payload


def gradient_resource(row: Any) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "artifact_id": int(row.artifact_id),
        "patch_id": str(row.patch_id),
        "iteration": int(row.iteration),
        "source_badcase_id": row.source_badcase_id,
        "tag_key": row.tag_key,
        "failure_stage": row.failure_stage,
        "failure_mode": row.failure_mode,
        "gradient_text": str(row.gradient_text),
        "proposed_edit": str(row.proposed_edit),
        "decision": str(row.decision),
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "decision_note": row.decision_note,
        "evaluation": dict(row.evaluation or {}),
    }
