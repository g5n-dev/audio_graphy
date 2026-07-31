"""Strict public contracts for tag governance."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FrozenStrictModel(BaseModel):
    """Immutable public contract used by versioned execution specifications."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


TruthState = Literal["present", "absent", "not_applicable", "uncertain"]
TruthTier = Literal["t0", "t1", "t2", "t3"]
FailureStage = Literal[
    "vad",
    "asr",
    "speaker",
    "boundary",
    "schema",
    "tag_reasoning",
    "evidence",
    "fusion",
    "insufficient_audio",
]
RegisteredHarnessTool = Literal["rule_engine", "weak_llm", "strong_llm"]
GoldTruthTier = Literal["t2", "t3"]
TagSubjectType = Literal["dialogue_unit", "reception"]


class TagDefinition(StrictModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=128)
    value_type: Literal["enum", "string", "number", "boolean"]
    allowed_values: list[Any] = Field(default_factory=list, max_length=256)
    subject_types: list[Literal["dialogue_unit", "reception"]] = Field(
        min_length=1,
        max_length=3,
    )
    scenarios: list[Literal["gold", "automotive", "custom"]] = Field(
        default_factory=list,
        max_length=3,
    )
    evidence_required: bool = True
    critical: bool = False
    critical_values: list[Any] = Field(default_factory=list, max_length=256)
    negative_values: list[Any] = Field(default_factory=list, max_length=256)
    required: bool = False
    threshold: float = Field(default=0.7, ge=0, le=1)
    mutually_exclusive_with: list[str] = Field(default_factory=list, max_length=64)
    depends_on: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_value_policy(self) -> Self:
        if self.value_type == "enum" and not self.allowed_values:
            raise ValueError("enum definitions require allowed_values")
        if (self.critical_values or self.negative_values) and self.value_type != "enum":
            raise ValueError("critical_values and negative_values require an enum definition")
        for field_name, values in (
            ("critical_values", self.critical_values),
            ("negative_values", self.negative_values),
        ):
            if len(values) != len({repr(value) for value in values}):
                raise ValueError(f"{field_name} must be unique")
            unknown = [value for value in values if value not in self.allowed_values]
            if unknown:
                raise ValueError(f"{field_name} must be a subset of allowed_values")
        if any(value in self.negative_values for value in self.critical_values):
            raise ValueError("critical_values and negative_values must be disjoint")
        if len(self.subject_types) != len(set(self.subject_types)):
            raise ValueError("subject_types must be unique")
        for field_name, references in (
            ("mutually_exclusive_with", self.mutually_exclusive_with),
            ("depends_on", self.depends_on),
        ):
            if len(references) != len(set(references)):
                raise ValueError(f"{field_name} must be unique")
            if self.key in references:
                raise ValueError(f"{field_name} cannot reference the tag itself")
            if any(
                not reference
                or len(reference) > 128
                or not all(character.isalnum() or character in "_.-" for character in reference)
                for reference in references
            ):
                raise ValueError(f"{field_name} contains an invalid tag key")
        return self


class TagSchemaCreate(StrictModel):
    key: str = Field(min_length=1, max_length=96, pattern=r"^[\w.-]+$")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4_000)


class TagSchemaVersionCreate(StrictModel):
    version: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    definitions: list[TagDefinition] = Field(min_length=1, max_length=256)


class HarnessContextSpec(StrictModel):
    neighbor_units: Literal[0, 1, 2] = 0
    example_policy: Literal["none", "similar", "hard_negative", "mixed"] = "none"
    example_top_k: Literal[0, 3, 6] = 0

    @model_validator(mode="after")
    def validate_examples(self) -> Self:
        if self.example_policy == "none" and self.example_top_k != 0:
            raise ValueError("example_policy=none requires example_top_k=0")
        if self.example_policy != "none" and self.example_top_k == 0:
            raise ValueError("example retrieval requires example_top_k=3 or 6")
        return self


class HarnessToolSpec(StrictModel):
    registered_tools: list[RegisteredHarnessTool] = Field(
        default_factory=lambda: cast(
            list[RegisteredHarnessTool],
            ["rule_engine", "weak_llm", "strong_llm"],
        )
    )
    primary_model: Literal["weak", "strong"] = "weak"
    critic_model: Literal["strong"] | None = None

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        if self.registered_tools != ["rule_engine", "weak_llm", "strong_llm"]:
            raise ValueError("registered_tools is fixed for Harness v1")
        return self


class HarnessGenerationSpec(StrictModel):
    temperature: Literal[0] = 0
    max_tokens: Literal[1024, 2048] = 2048
    response_format: Literal["strict_json"] = "strict_json"
    prompt_template: str = Field(default="", max_length=64_000)


class HarnessOrchestrationSpec(StrictModel):
    route: Literal[
        "rule_only",
        "weak_llm",
        "weak_then_strong_critic",
        "rule_llm_fusion",
    ] = "rule_llm_fusion"
    fusion_policy: Literal[
        "rule_priority",
        "score_priority",
        "conflict_to_review",
    ] = "conflict_to_review"
    critic_enabled: bool = False
    rule_bundle: dict[str, Any] = Field(default_factory=dict)


class HarnessMemorySpec(StrictModel):
    policy: Literal["none", "approved_cases"] = "none"
    top_k: Literal[0, 3, 6] = 0

    @model_validator(mode="after")
    def validate_retrieval(self) -> Self:
        if self.policy == "none" and self.top_k != 0:
            raise ValueError("memory policy=none requires top_k=0")
        if self.policy == "approved_cases" and self.top_k == 0:
            raise ValueError("approved_cases requires top_k=3 or 6")
        return self


class HarnessOutputSpec(StrictModel):
    thresholds: dict[str, float] = Field(default_factory=dict)
    fallback: Literal["review", "abstain", "rule"] = "review"
    schema_validation: bool = True
    evidence_validation: bool = True
    abstain_threshold: float = Field(default=0, ge=0, le=1)
    review_threshold: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        if self.abstain_threshold > self.review_threshold:
            raise ValueError("abstain_threshold cannot exceed review_threshold")
        for key, value in self.thresholds.items():
            if not key or len(key) > 128:
                raise ValueError("thresholds contains an invalid tag key")
            if not 0 <= value <= 1:
                raise ValueError("thresholds values must be between 0 and 1")
        return self


class HarnessSpecV1(StrictModel):
    spec_version: Literal["1.0"] = "1.0"
    context: HarnessContextSpec
    tools: HarnessToolSpec
    generation: HarnessGenerationSpec
    orchestration: HarnessOrchestrationSpec
    memory: HarnessMemorySpec
    output: HarnessOutputSpec

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.orchestration.route == "weak_then_strong_critic" and (
            not self.orchestration.critic_enabled or self.tools.critic_model != "strong"
        ):
            raise ValueError(
                "weak_then_strong_critic requires critic_enabled and strong critic_model"
            )
        return self


class HarnessContextSpecV2(HarnessContextSpec):
    model_config = FrozenStrictModel.model_config


class HarnessToolSpecV2(HarnessToolSpec):
    model_config = FrozenStrictModel.model_config


class HarnessBudgetPolicy(FrozenStrictModel):
    """Optional hard limits; ``None`` means the enclosing job owns the limit."""

    max_provider_tokens: int | None = Field(default=None, ge=1)
    max_provider_calls: int | None = Field(default=None, ge=1)
    max_cost_microunits: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)


class HarnessGenerationSpecV2(FrozenStrictModel):
    temperature: Literal[0] = 0
    max_input_tokens: Literal[12_000] = 12_000
    max_tokens: Literal[256, 512, 1024, 2048] = 2048
    response_format: Literal["strict_json"] = "strict_json"
    prompt_template: str = Field(default="", max_length=64_000)
    budget_policy: HarnessBudgetPolicy = Field(default_factory=HarnessBudgetPolicy)


class HarnessOrchestrationSpecV2(HarnessOrchestrationSpec):
    model_config = FrozenStrictModel.model_config

    rule_min_confidence: float = Field(default=0.95, ge=0, le=1)
    critic_confidence_margin: float = Field(default=0.10, ge=0, le=1)
    critic_max_noncritical_rate: float = Field(default=0.20, ge=0, le=1)

    @model_validator(mode="after")
    def validate_v2_policy_constants(self) -> Self:
        expected = {
            "rule_min_confidence": 0.95,
            "critic_confidence_margin": 0.10,
            "critic_max_noncritical_rate": 0.20,
        }
        for field_name, expected_value in expected.items():
            if getattr(self, field_name) != expected_value:
                raise ValueError(f"{field_name} is fixed to {expected_value:g} in Harness v2")
        return self


class HarnessMemorySpecV2(HarnessMemorySpec):
    model_config = FrozenStrictModel.model_config


class HarnessOutputSpecV2(HarnessOutputSpec):
    model_config = FrozenStrictModel.model_config


class HarnessSpecV2(FrozenStrictModel):
    """Quality-preserving bounded Harness contract for token-aware tagging."""

    spec_version: Literal["2.0"] = "2.0"
    context: HarnessContextSpecV2
    tools: HarnessToolSpecV2
    generation: HarnessGenerationSpecV2
    orchestration: HarnessOrchestrationSpecV2
    memory: HarnessMemorySpecV2
    output: HarnessOutputSpecV2

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.orchestration.route == "weak_then_strong_critic" and (
            not self.orchestration.critic_enabled or self.tools.critic_model != "strong"
        ):
            raise ValueError(
                "weak_then_strong_critic requires critic_enabled and strong critic_model"
            )
        return self


HarnessSpec = Annotated[
    HarnessSpecV1 | HarnessSpecV2,
    Field(discriminator="spec_version"),
]


class TaggerVersionCreate(StrictModel):
    schema_version_id: int = Field(gt=0)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    engine: Literal["rule", "llm", "hybrid"] = "hybrid"
    prompt_content: str = Field(default="", max_length=64_000)
    rule_bundle: dict[str, Any] = Field(default_factory=dict)
    model_version: str = Field(min_length=1, max_length=128)
    thresholds: dict[str, float] = Field(default_factory=dict)
    harness_spec: HarnessSpec | None = None
    parent_version_id: int | None = Field(default=None, gt=0)
    change_summary: str | None = Field(default=None, max_length=4_000)


class TagJobCreate(StrictModel):
    # Internal review/evaluation/optimization/remediation jobs carry trusted
    # lineage and must only be created by their dedicated server-side workflows.
    job_type: Literal["extract", "recompute"]
    scope: dict[str, Any]


class ReviewSubject(StrictModel):
    subject_type: Literal["dialogue_unit", "reception"]
    subject_id: int = Field(gt=0)
    reception_id: int | None = Field(default=None, gt=0)
    tag_key: str = Field(min_length=1, max_length=128)
    proposed_value: Any = None
    proposed_fact_id: int | None = Field(default=None, gt=0)
    schema_version_id: int | None = Field(default=None, gt=0)
    tagger_version_id: int | None = Field(default=None, gt=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    priority: int = Field(default=0, ge=-1_000, le=1_000)


class ReviewBatchCreate(StrictModel):
    reason: Literal[
        "conflict",
        "missing",
        "low_confidence",
        "critical",
        "random",
        "drift",
        "audit",
        "gold",
        "active_learning",
    ]
    subjects: list[ReviewSubject] = Field(min_length=1, max_length=1_000)
    review_bundle_id: str | None = Field(default=None, min_length=1, max_length=64)


class ReviewDecisionCreate(StrictModel):
    action: Literal["accept", "correct", "reject", "uncertain", "escalate"]
    corrected_value: Any = None
    reason_code: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=4_000)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    truth_state: TruthState | None = None
    primary_failure_stage: FailureStage | None = None
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    reviewer_confidence: float | None = Field(default=None, ge=0, le=1)
    review_duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)

    @model_validator(mode="after")
    def validate_truth_state(self) -> Self:
        if self.action == "uncertain" and self.truth_state not in {None, "uncertain"}:
            raise ValueError("uncertain action requires uncertain truth_state")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")
        if any(not code or len(code) > 64 for code in self.reason_codes):
            raise ValueError("reason_codes contains an invalid code")
        return self


class ReviewReleaseCreate(StrictModel):
    force: bool = False


class GoldSetCreate(StrictModel):
    key: str = Field(min_length=1, max_length=96, pattern=r"^[\w.-]+$")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4_000)
    schema_version_id: int = Field(gt=0)


class GoldFreezeCohort(StrictModel):
    review_bundle_ids: list[str] = Field(min_length=1, max_length=1_000)
    truth_tiers: list[GoldTruthTier] = Field(
        default_factory=lambda: cast(list[GoldTruthTier], ["t2", "t3"]),
        min_length=1,
        max_length=2,
    )
    subject_types: list[TagSubjectType] = Field(
        default_factory=lambda: cast(
            list[TagSubjectType],
            ["dialogue_unit", "reception"],
        ),
        min_length=1,
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_unique_values(self) -> Self:
        if len(self.review_bundle_ids) != len(set(self.review_bundle_ids)):
            raise ValueError("review_bundle_ids must be unique")
        if len(self.truth_tiers) != len(set(self.truth_tiers)):
            raise ValueError("truth_tiers must be unique")
        if len(self.subject_types) != len(set(self.subject_types)):
            raise ValueError("subject_types must be unique")
        return self


class GoldCompletenessChecklist(StrictModel):
    full_applicable_matrix: Literal[True]
    frozen_input_snapshots: Literal[True]
    reception_level_isolation: Literal[True]
    t2_t3_truth_only: Literal[True]


class GoldSetFreeze(StrictModel):
    version: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    cohort: GoldFreezeCohort
    completeness_checklist: GoldCompletenessChecklist


class TagEvaluationCreate(StrictModel):
    tagger_version_id: int = Field(gt=0)
    gold_set_version_id: int = Field(gt=0)
    baseline_tagger_version_id: int = Field(gt=0)


class TagDeploymentCreate(StrictModel):
    tagger_version_id: int = Field(gt=0)
    evaluation_run_id: int = Field(gt=0)
    baseline_tagger_version_id: int = Field(gt=0)


class TagRollbackCreate(StrictModel):
    reason: str = Field(default="manual", min_length=1, max_length=4_000)


class TagDeploymentResumeCreate(StrictModel):
    reason: str = Field(min_length=8, max_length=4_000)


class OptimizationCreate(StrictModel):
    gold_set_version_id: int = Field(gt=0)
    production_tagger_version_id: int = Field(gt=0)


class OptimizationSearchBudget(StrictModel):
    max_trials: int = Field(default=32, ge=1, le=32)
    sealed_holdout_queries: Literal[1] = 1
    max_provider_tokens: int | None = Field(default=None, ge=1)
    max_provider_calls: int | None = Field(default=None, ge=1)
    max_cost_microunits: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)


class OptimizationCohortFilters(StrictModel):
    store_ids: list[str] = Field(default_factory=list, max_length=500)
    agent_names: list[str] = Field(default_factory=list, max_length=500)
    reception_ids: list[int] = Field(default_factory=list, max_length=10_000)
    scenarios: list[Literal["gold", "automotive", "custom"]] = Field(
        default_factory=list,
        max_length=3,
    )
    group_keys: list[str] = Field(default_factory=list, max_length=500)
    label_keys: list[str] = Field(default_factory=list, max_length=500)
    started_from: str | None = Field(default=None, max_length=64)
    started_to: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_filter_values(self) -> Self:
        for field_name in (
            "store_ids",
            "agent_names",
            "reception_ids",
            "scenarios",
            "group_keys",
            "label_keys",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if any(value <= 0 for value in self.reception_ids):
            raise ValueError("reception_ids must be positive")
        return self


class OptimizationCohort(StrictModel):
    source: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    filters: OptimizationCohortFilters = Field(default_factory=OptimizationCohortFilters)
    group_ids: list[str] = Field(default_factory=list, max_length=1_000)
    conflict_only: bool = False

    @model_validator(mode="after")
    def validate_group_ids(self) -> Self:
        if len(self.group_ids) != len(set(self.group_ids)):
            raise ValueError("group_ids must be unique")
        return self


class OptimizationObjective(StrictModel):
    policy: Literal["balanced", "quality_first", "efficiency_guarded"]


class OptimizationRunCreate(StrictModel):
    cohort: OptimizationCohort
    target_policy: OptimizationObjective
    search_budget: OptimizationSearchBudget = Field(default_factory=OptimizationSearchBudget)


class OptimizationCandidateCompare(StrictModel):
    left_trial_id: int = Field(gt=0)
    right_trial_id: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_distinct_trials(self) -> Self:
        if self.left_trial_id == self.right_trial_id:
            raise ValueError("left_trial_id and right_trial_id must differ")
        return self


class OptimizationRunState(StrictModel):
    id: int = Field(gt=0)
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    phase: Literal[
        "prepare",
        "search",
        "validation",
        "challenge",
        "holdout",
        "completed",
    ]
    summary: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[Any] = Field(default_factory=list)
    artifacts: list[Any] = Field(default_factory=list)


__all__ = [
    "FailureStage",
    "FrozenStrictModel",
    "GoldCompletenessChecklist",
    "GoldFreezeCohort",
    "GoldSetCreate",
    "GoldSetFreeze",
    "HarnessBudgetPolicy",
    "HarnessContextSpec",
    "HarnessContextSpecV2",
    "HarnessGenerationSpec",
    "HarnessGenerationSpecV2",
    "HarnessMemorySpec",
    "HarnessMemorySpecV2",
    "HarnessOrchestrationSpec",
    "HarnessOrchestrationSpecV2",
    "HarnessOutputSpec",
    "HarnessOutputSpecV2",
    "HarnessSpec",
    "HarnessSpecV1",
    "HarnessSpecV2",
    "HarnessToolSpec",
    "HarnessToolSpecV2",
    "OptimizationCandidateCompare",
    "OptimizationCohort",
    "OptimizationCohortFilters",
    "OptimizationCreate",
    "OptimizationObjective",
    "OptimizationRunCreate",
    "OptimizationRunState",
    "OptimizationSearchBudget",
    "ReviewBatchCreate",
    "ReviewDecisionCreate",
    "ReviewReleaseCreate",
    "ReviewSubject",
    "StrictModel",
    "TagDefinition",
    "TagDeploymentCreate",
    "TagDeploymentResumeCreate",
    "TagEvaluationCreate",
    "TagJobCreate",
    "TagRollbackCreate",
    "TagSchemaCreate",
    "TagSchemaVersionCreate",
    "TaggerVersionCreate",
    "TruthState",
    "TruthTier",
]
