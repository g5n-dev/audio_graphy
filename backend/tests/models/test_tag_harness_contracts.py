"""Contract tests for the semantic-tag Harness evolution data foundation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_graphy.models import (
    Base,
    TagBadcase,
    TagExperienceCase,
    TagFeedbackEvent,
    TagFeedbackLaneAssignment,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagOptimizationRun,
    TagOptimizationTrial,
)
from audio_graphy.models.tag_governance import (
    TagDeploymentObservation,
    TagEvaluationRun,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSetVersion,
    TagReviewDecision,
    TagReviewTask,
)
from audio_graphy.schemas.tag_governance import (
    GoldSetFreeze,
    HarnessBudgetPolicy,
    HarnessContextSpec,
    HarnessContextSpecV2,
    HarnessGenerationSpec,
    HarnessGenerationSpecV2,
    HarnessMemorySpec,
    HarnessMemorySpecV2,
    HarnessOrchestrationSpec,
    HarnessOrchestrationSpecV2,
    HarnessOutputSpec,
    HarnessOutputSpecV2,
    HarnessSpecV1,
    HarnessSpecV2,
    HarnessToolSpec,
    HarnessToolSpecV2,
    OptimizationRunCreate,
    OptimizationRunState,
    ReviewDecisionCreate,
    TagDefinition,
    TaggerVersionCreate,
)

NEW_TABLES = {
    "tag_harness_executions",
    "tag_harness_stage_traces",
    "tag_feedback_events",
    "tag_feedback_lane_assignments",
    "tag_evaluation_items",
    "tag_badcases",
    "tag_experience_cases",
    "tag_optimization_runs",
    "tag_optimization_trials",
}


def _harness_spec() -> HarnessSpecV1:
    return HarnessSpecV1(
        context=HarnessContextSpec(
            neighbor_units=1,
            example_policy="mixed",
            example_top_k=3,
        ),
        tools=HarnessToolSpec(
            registered_tools=["rule_engine", "weak_llm", "strong_llm"],
            primary_model="weak",
            critic_model="strong",
        ),
        generation=HarnessGenerationSpec(
            prompt_template="Return schema-valid semantic tags.",
            max_tokens=2048,
        ),
        orchestration=HarnessOrchestrationSpec(
            route="weak_then_strong_critic",
            fusion_policy="conflict_to_review",
            critic_enabled=True,
            rule_bundle={"dsl_version": "1", "rules": []},
        ),
        memory=HarnessMemorySpec(
            policy="approved_cases",
            top_k=3,
        ),
        output=HarnessOutputSpec(
            schema_validation=True,
            evidence_validation=True,
            thresholds={"intent": 0.7},
            abstain_threshold=0.4,
            review_threshold=0.7,
        ),
    )


def test_new_harness_models_register_expected_tables() -> None:
    assert NEW_TABLES.issubset(Base.metadata.tables)
    assert TagHarnessExecution.__tablename__ == "tag_harness_executions"
    assert TagHarnessStageTrace.__tablename__ == "tag_harness_stage_traces"
    assert TagFeedbackEvent.__tablename__ == "tag_feedback_events"
    assert TagFeedbackLaneAssignment.__tablename__ == "tag_feedback_lane_assignments"
    assert TagBadcase.__tablename__ == "tag_badcases"
    assert TagExperienceCase.__tablename__ == "tag_experience_cases"
    assert TagOptimizationRun.__tablename__ == "tag_optimization_runs"
    assert TagOptimizationTrial.__tablename__ == "tag_optimization_trials"
    assert "job_id" in TagOptimizationRun.__table__.columns
    assert "sealed_release_key" in TagOptimizationRun.__table__.columns
    assert {
        column.name
        for column in TagOptimizationRun.__table__.indexes
        if column.name == "ux_tag_optimization_runs_sealed_release"
    } == {"ux_tag_optimization_runs_sealed_release"}
    assert "dataset_split" in TagBadcase.__table__.columns
    assert "dataset_split" in TagExperienceCase.__table__.columns


def test_existing_models_expose_frozen_lineage_and_sampling_contracts() -> None:
    assert {
        "harness_spec_version",
        "harness_spec",
        "parent_version_id",
        "origin",
        "optimization_run_id",
        "change_summary",
    }.issubset(TaggerVersion.__table__.columns.keys())
    assert {
        "review_bundle_id",
        "selection_policy",
        "selection_policy_version",
        "sampling_probability",
        "blind_mode",
        "source_deployment_id",
        "source_extraction_run_id",
        "source_harness_execution_id",
    }.issubset(TagReviewTask.__table__.columns.keys())
    assert {
        "truth_state",
        "truth_tier",
        "annotator_round",
        "primary_failure_stage",
        "reason_codes",
        "reviewer_confidence",
        "review_duration_ms",
    }.issubset(TagReviewDecision.__table__.columns.keys())
    assert {
        "dataset_snapshot_hash",
        "completeness_manifest",
    }.issubset(TagGoldSetVersion.__table__.columns.keys())
    assert {
        "truth_state",
        "truth_tier",
        "input_hash",
        "input_snapshot",
        "annotation_quality",
        "cohort",
        "completeness_manifest",
    }.issubset(TagGoldLabel.__table__.columns.keys())
    assert {
        "evaluator_version",
        "dataset_snapshot_hash",
    }.issubset(TagEvaluationRun.__table__.columns.keys())
    assert {
        "source",
        "provenance",
        "is_trusted",
        "served_count",
        "paired_count",
        "audited_count",
        "adjudicated_count",
    }.issubset(TagDeploymentObservation.__table__.columns.keys())


def test_harness_spec_is_six_dimensional_and_serializable() -> None:
    spec = _harness_spec()

    assert spec.spec_version == "1.0"
    assert spec.context.example_top_k == 3
    assert spec.tools.critic_model == "strong"
    assert spec.generation.temperature == 0
    assert spec.orchestration.route == "weak_then_strong_critic"
    assert spec.memory.top_k == 3
    assert spec.output.abstain_threshold == 0.4
    assert set(spec.model_dump()) == {
        "spec_version",
        "context",
        "tools",
        "generation",
        "orchestration",
        "memory",
        "output",
    }


def test_harness_spec_rejects_unbounded_search_values() -> None:
    with pytest.raises(ValidationError):
        HarnessContextSpec(example_top_k=4)
    with pytest.raises(ValidationError):
        HarnessContextSpec(neighbor_units=3)
    with pytest.raises(ValidationError):
        HarnessGenerationSpec(
            prompt_template="deterministic",
            temperature=0.2,
        )


def test_harness_v2_is_frozen_and_exposes_quality_preserving_token_policy() -> None:
    spec = HarnessSpecV2(
        context=HarnessContextSpecV2(),
        tools=HarnessToolSpecV2(),
        generation=HarnessGenerationSpecV2(
            max_input_tokens=12_000,
            max_tokens=512,
            prompt_template="Return grounded semantic tags.",
            budget_policy=HarnessBudgetPolicy(
                max_provider_tokens=50_000,
                max_provider_calls=50,
                max_cost_microunits=250_000,
                max_wall_seconds=900,
            ),
        ),
        orchestration=HarnessOrchestrationSpecV2(
            route="rule_llm_fusion",
            rule_min_confidence=0.95,
            critic_confidence_margin=0.10,
            critic_max_noncritical_rate=0.20,
        ),
        memory=HarnessMemorySpecV2(),
        output=HarnessOutputSpecV2(),
    )

    assert spec.spec_version == "2.0"
    assert spec.generation.max_tokens == 512
    assert spec.generation.budget_policy.max_provider_tokens == 50_000
    assert spec.orchestration.rule_min_confidence == pytest.approx(0.95)
    with pytest.raises(ValidationError, match="frozen"):
        spec.generation.max_tokens = 2048


def test_harness_v2_rejects_unbounded_generation_and_critic_policies() -> None:
    with pytest.raises(ValidationError):
        HarnessGenerationSpecV2(max_input_tokens=16_000)
    with pytest.raises(ValidationError):
        HarnessGenerationSpecV2(max_tokens=768)
    with pytest.raises(ValidationError):
        HarnessOrchestrationSpecV2(rule_min_confidence=0.94)
    with pytest.raises(ValidationError):
        HarnessOrchestrationSpecV2(critic_max_noncritical_rate=0.21)


def test_tagger_create_accepts_harness_while_legacy_contract_remains_valid() -> None:
    legacy = TaggerVersionCreate(
        schema_version_id=1,
        version="legacy-v1",
        engine="hybrid",
        prompt_content="legacy prompt",
        model_version="weak-v1",
    )
    evolved = TaggerVersionCreate(
        schema_version_id=1,
        version="harness-v1",
        engine="hybrid",
        prompt_content="versioned prompt",
        model_version="weak-v1",
        harness_spec=_harness_spec(),
        parent_version_id=9,
        change_summary="add strong-model critic",
    )

    assert legacy.harness_spec is None
    assert evolved.harness_spec is not None
    assert evolved.parent_version_id == 9

    evolved_v2 = TaggerVersionCreate(
        schema_version_id=1,
        version="harness-v2",
        engine="hybrid",
        prompt_content="versioned prompt",
        model_version="weak-v2",
        harness_spec=HarnessSpecV2(
            context=HarnessContextSpecV2(),
            tools=HarnessToolSpecV2(),
            generation=HarnessGenerationSpecV2(prompt_template="versioned prompt"),
            orchestration=HarnessOrchestrationSpecV2(),
            memory=HarnessMemorySpecV2(),
            output=HarnessOutputSpecV2(),
        ),
    )
    assert evolved_v2.harness_spec is not None
    assert evolved_v2.harness_spec.spec_version == "2.0"

    with pytest.raises(ValidationError, match="origin"):
        TaggerVersionCreate(
            schema_version_id=1,
            version="poisoned-harness-v1",
            engine="hybrid",
            prompt_content="versioned prompt",
            model_version="weak-v1",
            origin="optimizer",
        )


def test_tag_definition_validates_critical_and_negative_values() -> None:
    definition = TagDefinition(
        key="compliance_risk",
        name="Compliance risk",
        category="risk",
        value_type="enum",
        allowed_values=["none", "privacy", "overpromise"],
        critical_values=["privacy", "overpromise"],
        negative_values=["none"],
        subject_types=["dialogue_unit", "reception"],
    )

    assert definition.critical_values == ["privacy", "overpromise"]
    assert definition.negative_values == ["none"]

    with pytest.raises(ValidationError, match="critical_values"):
        TagDefinition(
            key="risk",
            name="Risk",
            category="risk",
            value_type="enum",
            allowed_values=["none", "privacy"],
            critical_values=["unknown"],
            subject_types=["dialogue_unit"],
        )
    with pytest.raises(ValidationError, match="disjoint"):
        TagDefinition(
            key="risk",
            name="Risk",
            category="risk",
            value_type="enum",
            allowed_values=["none", "privacy"],
            critical_values=["privacy"],
            negative_values=["privacy"],
            subject_types=["dialogue_unit"],
        )


def test_review_decision_contract_carries_truth_quality_and_diagnosis() -> None:
    decision = ReviewDecisionCreate(
        action="correct",
        corrected_value="privacy",
        reason_code="asr_homophone",
        truth_state="present",
        primary_failure_stage="asr",
        reason_codes=["asr_homophone", "missing_evidence"],
        reviewer_confidence=0.9,
        review_duration_ms=12_000,
    )

    assert decision.truth_state == "present"
    assert decision.primary_failure_stage == "asr"
    assert decision.reason_codes == ["asr_homophone", "missing_evidence"]
    with pytest.raises(ValidationError):
        ReviewDecisionCreate.model_validate(
            {
                **decision.model_dump(),
                "truth_tier": "t3",
                "annotator_round": 3,
                "adjudication": True,
            }
        )


def test_optimization_contract_bounds_trials_and_exposes_agent_state() -> None:
    request = OptimizationRunCreate(
        cohort={
            "source": "tag_insights",
            "filters": {"scenarios": ["gold"]},
        },
        target_policy={"policy": "quality_first"},
    )
    state = OptimizationRunState(
        id=7,
        status="running",
        phase="validation",
        summary={"completed_trials": 4},
        next_actions=[{"type": "evaluate_challenge"}],
        artifacts=[{"type": "candidate_diff", "id": "artifact-1"}],
    )

    assert request.search_budget.max_trials == 32
    assert request.search_budget.sealed_holdout_queries == 1
    assert not hasattr(request, "gold_set_version_id")
    assert not hasattr(request, "trigger")
    assert state.status == "running"
    assert state.summary["completed_trials"] == 4

    with pytest.raises(ValidationError):
        OptimizationRunCreate(
            cohort={"source": "tag_insights"},
            target_policy={"policy": "quality_first"},
            search_budget={"max_trials": 33},
        )
    with pytest.raises(ValidationError):
        OptimizationRunCreate.model_validate(
            {
                "gold_set_version_id": 3,
                "cohort": {"source": "tag_insights"},
                "target_policy": {"policy": "quality_first"},
            }
        )


def test_gold_freeze_accepts_only_server_resolved_cohorts_and_complete_checklist() -> None:
    request = GoldSetFreeze(
        version="2026.07",
        cohort={
            "review_bundle_ids": ["release-2026-07"],
            "truth_tiers": ["t2", "t3"],
            "subject_types": ["dialogue_unit", "reception"],
        },
        completeness_checklist={
            "full_applicable_matrix": True,
            "frozen_input_snapshots": True,
            "reception_level_isolation": True,
            "t2_t3_truth_only": True,
        },
    )

    assert request.cohort.review_bundle_ids == ["release-2026-07"]
    with pytest.raises(ValidationError):
        GoldSetFreeze.model_validate(
            {
                "version": "poisoned",
                "decision_ids": [1, 2, 3],
                "cohort": {
                    "review_bundle_ids": ["release-2026-07"],
                },
                "completeness_checklist": {
                    "full_applicable_matrix": True,
                    "frozen_input_snapshots": True,
                    "reception_level_isolation": True,
                    "t2_t3_truth_only": True,
                },
            }
        )


def test_optimization_trigger_survives_orm_serialization() -> None:
    run = TagOptimizationRun(
        tenant_id="tenant-a",
        baseline_tagger_version_id=1,
        gold_set_version_id=2,
        dataset_snapshot_hash="a" * 64,
        trigger="insight",
        cohort={},
        objective={},
        search_budget={"max_trials": 32},
        summary={},
        next_actions=[],
        artifacts=[],
        created_by=3,
    )

    assert run.to_dict()["trigger"] == "insight"


def test_optimization_trials_can_persist_explicit_cancellation() -> None:
    constraint_sql = " ".join(
        str(constraint.sqltext)
        for constraint in TagOptimizationTrial.__table__.constraints
        if constraint.name == "ck_tag_optimization_trials_status"
    )

    assert "cancelled" in constraint_sql
