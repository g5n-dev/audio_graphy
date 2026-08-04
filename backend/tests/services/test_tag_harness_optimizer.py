"""Pure contracts for the bounded, server-side Harness optimizer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base


@pytest.fixture
async def optimizer_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _gold_lane_labels(
    *,
    gold_set_version_id: int,
    subject_ids: tuple[int, int, int, int],
    reception_id: int | None = None,
    missing_lane: str | None = None,
    review_decision_base: int = 1_000_000,
    positive_support: int = 73,
    absent_support: int = 30,
) -> list[Any]:
    from audio_graphy.models.tag_governance import TagGoldLabel

    rows: list[tuple[str, str, str, str, str | None, int]] = [
        ("train", "train", "t2", "present", "purchase", subject_ids[0]),
        ("validation", "validation", "t2", "present", "purchase", subject_ids[1]),
    ]
    rows.extend(
        (
            ("holdout_t3_present" if index < positive_support else "holdout_t3_absent"),
            "holdout",
            "t3",
            ("present" if index < positive_support else "absent"),
            ("purchase" if index < positive_support else None),
            subject_ids[2] + index,
        )
        for index in range(positive_support + absent_support)
    )
    return [
        TagGoldLabel(
            tenant_id="chang_an",
            gold_set_version_id=gold_set_version_id,
            review_decision_id=review_decision_base + index,
            reception_id=reception_id,
            subject_type="dialogue_unit",
            subject_id=subject_id,
            tag_key="intent",
            tag_value=tag_value,
            evidence_refs=[],
            truth_state=truth_state,
            truth_tier=truth_tier,
            input_hash=f"{index:064x}",
            input_snapshot={},
            annotation_quality={},
            cohort="optimizer-preflight",
            completeness_manifest={"complete": True},
            split=split,
        )
        for index, (lane, split, truth_tier, truth_state, tag_value, subject_id) in enumerate(
            rows,
            start=1,
        )
        if lane != missing_lane
    ]


def _trial_evaluator(
    candidate: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, float | bool]:
    assert all(sample["primary_failure_stage"] == "tag_reasoning" for sample in samples)
    max_tokens = int(candidate["generation"]["max_tokens"])
    return {
        "feasible": max_tokens != 256,
        "quality_delta": 100.0 if max_tokens == 256 else (2048 - max_tokens) / 10_000,
        "review_rate_delta": 0.02 if max_tokens == 2048 else -0.01,
        "p95_latency_delta": float(max_tokens / 100),
        "cost_delta": float(max_tokens / 2048),
    }


def test_bounded_search_is_deterministic_capped_and_lexicographic() -> None:
    from types import SimpleNamespace

    from audio_graphy.services.tag_governance import bounded_harness_search
    from audio_graphy.services.tag_harness_runtime import resolve_harness_spec

    samples = [
        {"primary_failure_stage": "tag_reasoning", "subject_id": 1},
        {"primary_failure_stage": "asr", "subject_id": 2},
        {"primary_failure_stage": "speaker", "subject_id": 3},
    ]
    baseline = {
        "context": {"neighbor_units": 0},
        "memory": {"example_count": 0, "strategy": "similar"},
        "orchestration": {"route": "weak_llm", "fusion": "score_priority"},
        "output": {"threshold_offset": 0.0},
    }

    first = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=samples,
        evaluator=_trial_evaluator,
        max_candidates=32,
    )
    second = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=list(reversed(samples)),
        evaluator=_trial_evaluator,
        max_candidates=32,
    )

    assert 1 < len(first.trials) <= 32
    assert first.excluded_upstream_count == 2
    assert first.eligible_sample_count == 1
    assert first.winner.reward.feasible is True
    assert first.winner.config["generation"]["max_tokens"] == 512
    assert [trial.config for trial in first.trials] == [trial.config for trial in second.trials]
    assert first.winner.config == second.winner.config
    assert not any(trial.mutation.startswith(("context.", "memory.")) for trial in first.trials)
    for trial in first.trials:
        resolved = resolve_harness_spec(
            SimpleNamespace(
                harness_spec=trial.config,
                engine="hybrid",
                prompt_content="prompt",
                rule_bundle={"dsl_version": "1", "rules": []},
                thresholds={"intent": 0.7},
            )
        )
        assert resolved["context"]["example_top_k"] in {0, 3, 6}
        assert "fusion_policy" in resolved["orchestration"]
        assert "thresholds" in resolved["output"]


def test_optimizer_objectives_have_distinct_deterministic_ordering() -> None:
    from audio_graphy.services.tag_governance import bounded_harness_search

    def evaluator(
        candidate: dict[str, Any],
        _samples: list[dict[str, Any]],
    ) -> dict[str, float | bool]:
        max_tokens = int(candidate["generation"]["max_tokens"])
        review_threshold = float(candidate["output"]["review_threshold"])
        if review_threshold == 0.8:
            quality, cost = 1.0, 0.0
        elif max_tokens == 256:
            quality, cost = 0.0, -1.0
        elif max_tokens == 512:
            quality, cost = 0.8, -0.5
        else:
            quality, cost = 0.4, -0.1
        return {
            "feasible": True,
            "quality_delta": quality,
            "review_rate_delta": 0.0,
            "p95_latency_delta": 0.0,
            "cost_delta": cost,
        }

    baseline = {
        "generation": {"max_tokens": 2048},
        "output": {"review_threshold": 0.7},
    }
    quality = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=[],
        evaluator=evaluator,
        objective_policy="quality_first",
    )
    efficiency = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=[],
        evaluator=evaluator,
        objective_policy="efficiency_guarded",
    )
    balanced = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=[],
        evaluator=evaluator,
        objective_policy="balanced",
    )

    assert quality.winner.config["output"]["review_threshold"] == pytest.approx(0.8)
    assert efficiency.winner.config["generation"]["max_tokens"] == 256
    assert balanced.winner.config["generation"]["max_tokens"] == 512


def _compiled_candidate(baseline: Mapping[str, Any], *, prompt: str, patch_id: str) -> Any:
    from audio_graphy.services.tag_governance import InjectedCandidate
    from audio_graphy.services.tag_harness_runtime import materialize_trial_candidate

    return InjectedCandidate(
        mutation=f"generation.prompt_template=builtin#{patch_id}",
        config=materialize_trial_candidate(
            dict(baseline),
            prompt_mode="replace",
            prompt_template=prompt,
        ),
        provenance={"prompt_artifact_id": 7, "patch_ids": [patch_id]},
    )


def test_injected_candidates_survive_the_max_candidates_ceiling() -> None:
    """The sweep must be truncated before the compiled prompts a run exists to test."""

    from audio_graphy.services.tag_governance import _bounded_candidate_configs

    baseline = {
        "generation": {"max_tokens": 2048, "prompt_template": "基线规则"},
        "output": {"review_threshold": 0.7, "thresholds": {"intent": 0.7}},
    }
    injected = [
        _compiled_candidate(baseline, prompt="编译产物 A", patch_id="a1"),
        _compiled_candidate(baseline, prompt="编译产物 B", patch_id="b2"),
    ]

    unbounded = _bounded_candidate_configs(baseline, extra_candidates=injected)
    assert len(unbounded) > 3, "the mechanical sweep should still dominate the envelope"

    truncated = _bounded_candidate_configs(baseline, extra_candidates=injected)[:3]
    mutations = [mutation for mutation, _config in truncated]
    assert mutations[0] == "baseline"
    assert mutations[1:] == [
        "generation.prompt_template=builtin#a1",
        "generation.prompt_template=builtin#b2",
    ]
    prompts = [config["generation"]["prompt_template"] for _mutation, config in truncated]
    assert prompts == ["基线规则", "编译产物 A", "编译产物 B"]


def test_injected_candidate_order_is_independent_of_caller_ordering() -> None:
    from audio_graphy.services.tag_governance import _bounded_candidate_configs

    baseline = {"generation": {"max_tokens": 2048, "prompt_template": "基线规则"}}
    first = _compiled_candidate(baseline, prompt="编译产物 A", patch_id="a1")
    second = _compiled_candidate(baseline, prompt="编译产物 B", patch_id="b2")

    forward = _bounded_candidate_configs(baseline, extra_candidates=[first, second])
    reverse = _bounded_candidate_configs(baseline, extra_candidates=[second, first])

    assert forward == reverse


def test_injected_candidates_respect_materialized_dimensions() -> None:
    """An executor that cannot materialize generation must not be handed prompt work."""

    from audio_graphy.services.tag_governance import _bounded_candidate_configs

    baseline = {"generation": {"max_tokens": 2048, "prompt_template": "基线规则"}}
    injected = [_compiled_candidate(baseline, prompt="编译产物", patch_id="a1")]

    configs = _bounded_candidate_configs(
        baseline,
        materialized_dimensions=frozenset({"output"}),
        extra_candidates=injected,
    )

    assert all("prompt_template" not in mutation for mutation, _config in configs)


def _manifest_kwargs() -> dict[str, Any]:
    return {
        "dataset_snapshot_hash": "8" * 64,
        "baseline_tagger_version_id": 12,
        "baseline_config_checksum": "c" * 64,
        "schema_checksum": "d" * 64,
        "candidate_checksums": ["e" * 64],
        "gold_inputs": [{"gold_label_id": 1, "input_hash": "f" * 64}],
    }


def test_search_manifest_is_unchanged_without_injection() -> None:
    """A run admitted before the prompt compiler must resume against the same checksum."""

    from audio_graphy.services.tag_governance import _search_manifest_payload

    payload = _search_manifest_payload(**_manifest_kwargs())

    assert "injected_candidates" not in payload
    assert set(payload) == {
        "dataset_snapshot_hash",
        "baseline_tagger_version_id",
        "baseline_config_checksum",
        "schema_checksum",
        "candidate_checksums",
        "gold_inputs",
        "parser_version",
        "postprocessor_version",
        "cache_recipe_version",
    }


def test_search_manifest_records_injection_provenance() -> None:
    from audio_graphy.services.tag_governance import (
        _search_manifest_payload,
        canonical_checksum,
    )

    baseline = {"generation": {"max_tokens": 2048, "prompt_template": "基线规则"}}
    first = _compiled_candidate(baseline, prompt="编译产物 A", patch_id="a1")
    second = _compiled_candidate(baseline, prompt="编译产物 B", patch_id="b2")

    forward = _search_manifest_payload(
        **_manifest_kwargs(),
        extra_candidates=[first, second],
    )
    reverse = _search_manifest_payload(
        **_manifest_kwargs(),
        extra_candidates=[second, first],
    )

    assert forward == reverse, "manifest must not depend on caller ordering"
    assert [item["mutation"] for item in forward["injected_candidates"]] == [
        "generation.prompt_template=builtin#a1",
        "generation.prompt_template=builtin#b2",
    ]
    assert forward["injected_candidates"][0]["provenance"] == {
        "prompt_artifact_id": 7,
        "patch_ids": ["a1"],
    }
    assert canonical_checksum(forward) != canonical_checksum(
        _search_manifest_payload(**_manifest_kwargs())
    )


def test_bounded_search_evaluates_injected_candidates() -> None:
    from audio_graphy.services.tag_governance import bounded_harness_search

    baseline = {"generation": {"max_tokens": 2048, "prompt_template": "基线规则"}}
    compiled_prompt = "编译产物：仅在出现明确金额时输出价格标签"

    def evaluator(
        candidate: dict[str, Any],
        _samples: list[dict[str, Any]],
    ) -> dict[str, float | bool]:
        is_compiled = candidate["generation"]["prompt_template"] == compiled_prompt
        return {
            "feasible": True,
            "quality_delta": 0.5 if is_compiled else 0.0,
            "review_rate_delta": 0.0,
            "p95_latency_delta": 0.0,
            "cost_delta": 0.0,
        }

    result = bounded_harness_search(
        baseline_config=baseline,
        feedback_samples=[],
        evaluator=evaluator,
        objective_policy="quality_first",
        extra_candidates=[_compiled_candidate(baseline, prompt=compiled_prompt, patch_id="a1")],
    )

    assert result.winner.mutation == "generation.prompt_template=builtin#a1"
    assert result.winner.config["generation"]["prompt_template"] == compiled_prompt


@pytest.mark.asyncio
async def test_async_trial_executor_receives_only_materialized_dimensions_and_real_usage() -> None:
    from audio_graphy.services.tag_governance import execute_harness_trials

    class RecordingExecutor:
        materialized_dimensions = frozenset({"generation", "output"})

        def __init__(self) -> None:
            self.configs: list[dict[str, Any]] = []

        async def execute_trial(
            self,
            candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, Any]:
            self.configs.append(candidate)
            max_tokens = int(candidate["generation"]["max_tokens"])
            return {
                "measurement_source": "prediction_batch_replay",
                "measurement_complete": True,
                "provider_input_tokens": max_tokens,
                "provider_output_tokens": 10,
                "provider_calls": 1,
                "feasible": True,
                "quality_delta": 0.0,
                "review_rate_delta": 0.0,
                "p95_latency_delta": 0.0,
                "cost_delta": float(max_tokens),
            }

    executor = RecordingExecutor()
    result = await execute_harness_trials(
        baseline_config={
            "context": {"neighbor_units": 2},
            "generation": {"max_tokens": 2048},
            "memory": {"policy": "approved_cases", "top_k": 6},
        },
        feedback_samples=[],
        trial_executor=executor,
        objective_policy="efficiency_guarded",
        max_candidates=32,
    )

    assert executor.configs
    assert all(config["context"]["neighbor_units"] == 2 for config in executor.configs)
    assert all(
        config["memory"] == {"policy": "approved_cases", "top_k": 6} for config in executor.configs
    )
    assert not any(
        trial.mutation.startswith(("context.", "memory.", "orchestration."))
        for trial in result.trials
    )
    assert result.winner.metrics["measurement_source"] == "prediction_batch_replay"
    assert result.winner.config["generation"]["max_tokens"] == 256


@pytest.mark.asyncio
async def test_persisted_policy_replay_prefers_real_cost_microunits_with_legacy_fallback() -> None:
    from audio_graphy.services.tag_governance import PersistedPredictionTrialExecutor

    executor = PersistedPredictionTrialExecutor(baseline_thresholds={"intent": 0.7})
    candidate = {
        "output": {
            "thresholds": {"intent": 0.7},
            "review_threshold": 0.7,
        }
    }
    sample = {
        "split": "validation",
        "tag_key": "intent",
        "subject_tag_key": "dialogue_unit:intent",
        "score": 0.9,
        "is_correct": True,
        "is_critical": False,
        "harness_execution_id": 7,
        "provider_tokens": 100,
        "provider_cost_units": 999.0,
        "provider_cost_microunits": 123,
        "provider_latency_ms": 20,
    }
    real = await executor.execute_trial(candidate, [sample])
    assert real["measurement_complete"] is True
    assert real["provider_cost_microunits"] == 123
    assert real["provider_cost_units"] == 0
    assert real["cost_measurement_source"] == "price_snapshot_microunits"

    legacy_sample = {
        **sample,
        "harness_execution_id": 8,
        "provider_cost_microunits": None,
        "provider_cost_units": 0.25,
    }
    legacy = await executor.execute_trial(candidate, [legacy_sample])
    assert legacy["measurement_complete"] is True
    assert legacy["provider_cost_microunits"] is None
    assert legacy["provider_cost_units"] == pytest.approx(0.25)
    assert legacy["cost_measurement_source"] == "legacy_cost_units_compatibility"


@pytest.mark.asyncio
async def test_tag_extractor_trial_executor_replays_frozen_subjects_with_real_usage() -> None:
    from types import SimpleNamespace

    from audio_graphy.services.tag_extractor import TagExtractorHarnessTrialExecutor

    class RecordingPredictor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def predict_materialized_frozen_input(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return SimpleNamespace(
                assignments=(
                    {
                        "tag_key": "intent",
                        "tag_value": "purchase",
                        "confidence": 0.9,
                        "evidence_refs": [{"segment_id": 1}],
                    },
                ),
                review_items=(),
                latency_ms=25,
                provider_input_tokens=80,
                provider_output_tokens=20,
                reused_input_tokens=0,
                reused_output_tokens=0,
                provider_calls=1,
                cache_hits=0,
                strong_escalations=0,
                cost_microunits=15,
            )

    predictor = RecordingPredictor()
    executor = TagExtractorHarnessTrialExecutor(predictor)  # type: ignore[arg-type]
    candidate = {
        "generation": {
            "prompt_template": "真实候选提示词",
            "max_tokens": 256,
        },
        "orchestration": {
            "route": "weak_llm",
        },
        "output": {
            "thresholds": {"intent": 0.7},
        },
    }
    snapshot = {
        "subject_type": "dialogue_unit",
        "dialogue_unit_id": 10,
        "reception_id": 20,
        "schema_version_id": 30,
        "schema_checksum": "a" * 64,
        "scenario": "automotive",
        "transcript": "客户决定购买",
        "dialogue_unit_version": 1,
        "segments": [],
    }
    metrics = await executor.execute_trial(
        candidate,
        [
            {
                "tenant_id": "chang_an",
                "baseline_tagger_version_id": 40,
                "subject_type": "dialogue_unit",
                "subject_id": 10,
                "tag_key": "intent",
                "gold_value": "purchase",
                "truth_state": "present",
                "split": "validation",
                "is_critical": True,
                "baseline_predicted_value": "browse",
                "baseline_is_correct": False,
                "input_snapshot": snapshot,
                "harness_execution_id": 50,
                "provider_tokens": 140,
                "provider_cost_microunits": 20,
                "provider_cold_cost_microunits": 20,
                "provider_input_tokens": 110,
                "provider_output_tokens": 30,
                "reused_input_tokens": 0,
                "reused_output_tokens": 0,
                "provider_calls": 1,
                "cache_hits": 0,
                "unknown_billed_tokens": 0,
                "review_item_count": 0,
                "baseline_reviewed": False,
                "provider_latency_ms": 30,
            }
        ],
        optimization_run_id=61,
        optimization_trial_id=62,
    )

    assert len(predictor.calls) == 1
    assert predictor.calls[0]["input_snapshot"] == snapshot
    assert predictor.calls[0]["harness_spec"]["generation"]["prompt_template"] == ("真实候选提示词")
    usage_context = predictor.calls[0]["usage_context"]
    assert usage_context.optimization_run_id == 61
    assert usage_context.optimization_trial_id == 62
    assert usage_context.require_durable_ledger is True
    assert usage_context.logical_request_id.startswith("opt:61:62:")
    assert metrics["measurement_source"] == "tag_extractor_frozen_replay"
    assert metrics["measurement_complete"] is True
    assert metrics["provider_input_tokens"] == 80
    assert metrics["provider_output_tokens"] == 20
    assert metrics["cold_provider_tokens"] == 100
    assert metrics["provider_token_delta"] == -40
    assert metrics["cold_token_reduction"] == pytest.approx(2 / 7)
    assert metrics["paired_token_reduction_lcb"] == pytest.approx(2 / 7)
    assert metrics["cost_delta"] == -5
    assert metrics["cold_cost_reduction"] == pytest.approx(0.25)
    assert metrics["p95_latency_regression_rate"] == pytest.approx(-1 / 6)
    assert metrics["review_rate"] == 0
    assert metrics["baseline_review_rate"] == 0
    assert metrics["efficiency_gate_passed"] is True
    assert metrics["quality_delta"] > 0
    assert metrics["critical_recall"] == 1.0
    assert metrics["critical_recall_lcb"] < 0.95
    assert metrics["feasible"] is False
    assert metrics["efficiency_envelope"] == "token_reduction_v1"


def _grown_prompt_predictor(*, provider_calls: int) -> Any:
    """A candidate that spends more tokens than the baseline, as a longer prompt does."""

    from types import SimpleNamespace

    class Predictor:
        async def predict_materialized_frozen_input(self, **kwargs: Any) -> Any:
            del kwargs
            return SimpleNamespace(
                assignments=(
                    {
                        "tag_key": "intent",
                        "tag_value": "purchase",
                        "confidence": 0.9,
                        "evidence_refs": [{"segment_id": 1}],
                    },
                ),
                review_items=(),
                latency_ms=25,
                provider_input_tokens=130,
                provider_output_tokens=20,
                reused_input_tokens=0,
                reused_output_tokens=0,
                provider_calls=provider_calls,
                cache_hits=0,
                strong_escalations=0,
                cost_microunits=22,
            )

    return Predictor()


def _grown_prompt_sample() -> dict[str, Any]:
    return {
        "tenant_id": "chang_an",
        "baseline_tagger_version_id": 40,
        "subject_type": "dialogue_unit",
        "subject_id": 10,
        "tag_key": "intent",
        "gold_value": "purchase",
        "truth_state": "present",
        "split": "validation",
        "is_critical": True,
        "baseline_predicted_value": "browse",
        "baseline_is_correct": False,
        "input_snapshot": {
            "subject_type": "dialogue_unit",
            "dialogue_unit_id": 10,
            "reception_id": 20,
            "schema_version_id": 30,
            "schema_checksum": "a" * 64,
            "scenario": "automotive",
            "transcript": "客户决定购买",
            "dialogue_unit_version": 1,
            "segments": [],
        },
        "harness_execution_id": 50,
        "provider_tokens": 140,
        "provider_cost_microunits": 20,
        "provider_cold_cost_microunits": 20,
        "provider_input_tokens": 110,
        "provider_output_tokens": 30,
        "reused_input_tokens": 0,
        "reused_output_tokens": 0,
        "provider_calls": 1,
        "cache_hits": 0,
        "unknown_billed_tokens": 0,
        "review_item_count": 0,
        "baseline_reviewed": False,
        "provider_latency_ms": 30,
    }


def _budget_definitions() -> dict[str, dict[str, Any]]:
    return {
        "intent": {
            "key": "intent",
            "value_type": "enum",
            "allowed_values": ["purchase", "browse"],
        }
    }


def _generation(prompt: str, *, max_input_tokens: int = 12_000) -> dict[str, Any]:
    return {"generation": {"prompt_template": prompt, "max_input_tokens": max_input_tokens}}


def test_prompt_budget_report_charges_a_longer_prompt_against_segment_headroom() -> None:
    from audio_graphy.services.tag_extractor import prompt_input_budget_report

    baseline = _generation("基线规则")
    report = prompt_input_budget_report(
        _generation("编译产物：" * 400),
        baseline=baseline,
        definitions=_budget_definitions(),
    )

    assert report.fits is True
    assert report.prompt_tokens > 0
    assert report.schema_tokens > 0
    assert report.fixed_tokens == report.prompt_tokens + report.schema_tokens
    assert report.headroom_delta < 0, "a longer prompt must cost segment headroom"
    assert 0 < report.headroom_shrink_ratio < 1


def test_prompt_budget_report_is_neutral_for_the_baseline_itself() -> None:
    from audio_graphy.services.tag_extractor import prompt_input_budget_report

    baseline = _generation("基线规则")
    report = prompt_input_budget_report(
        baseline,
        baseline=baseline,
        definitions=_budget_definitions(),
    )

    assert report.headroom_delta == 0
    assert report.headroom_shrink_ratio == 0.0
    assert report.fits is True


def test_prompt_budget_report_flags_a_prompt_the_batcher_would_reject() -> None:
    """fits=False must agree with the guard inside _segment_batches_for_input_budget."""

    from audio_graphy.services.tag_extractor import (
        AssignmentValidationError,
        TagExtractor,
        prompt_input_budget_report,
    )

    definitions = _budget_definitions()
    overflowing = "超" * 12_000
    report = prompt_input_budget_report(
        _generation(overflowing),
        baseline=_generation("基线规则"),
        definitions=definitions,
    )

    assert report.fits is False
    assert report.headroom_tokens < 0
    # The preflight is only useful if it predicts the real failure, so assert the
    # production batcher actually rejects the same prompt.
    with pytest.raises(AssignmentValidationError, match="exceed the subject input token budget"):
        TagExtractor._segment_batches_for_input_budget(
            segment_texts=(),
            definitions=definitions,
            prompt_content=overflowing,
            max_input_tokens=12_000,
        )


@pytest.mark.asyncio
async def test_trial_reports_per_label_metrics_and_flipped_subjects() -> None:
    """The replay comparison view needs to name what regressed, not just an F1 delta."""

    from types import SimpleNamespace

    from audio_graphy.services.tag_extractor import TagExtractorHarnessTrialExecutor

    class Predictor:
        async def predict_materialized_frozen_input(self, **kwargs: Any) -> Any:
            subject_id = int(kwargs["input_snapshot"]["dialogue_unit_id"])
            # Subject 10 gets fixed, subject 11 gets broken by the candidate.
            tag_value = "purchase" if subject_id == 10 else "browse"
            return SimpleNamespace(
                assignments=(
                    {
                        "tag_key": "intent",
                        "tag_value": tag_value,
                        "confidence": 0.9,
                        "evidence_refs": [{"segment_id": 1}],
                    },
                ),
                review_items=(),
                latency_ms=25,
                provider_input_tokens=80,
                provider_output_tokens=20,
                reused_input_tokens=0,
                reused_output_tokens=0,
                provider_calls=1,
                cache_hits=0,
                strong_escalations=0,
                cost_microunits=15,
            )

    def sample(*, subject_id: int, baseline_value: str, baseline_correct: bool) -> dict[str, Any]:
        payload = _grown_prompt_sample()
        payload["subject_id"] = subject_id
        payload["input_snapshot"] = {
            **payload["input_snapshot"],
            "dialogue_unit_id": subject_id,
        }
        payload["baseline_predicted_value"] = baseline_value
        payload["baseline_is_correct"] = baseline_correct
        return payload

    executor = TagExtractorHarnessTrialExecutor(Predictor())  # type: ignore[arg-type]
    metrics = await executor.execute_trial(
        {"generation": {"prompt_template": "候选提示词", "max_tokens": 256}},
        [
            sample(subject_id=10, baseline_value="browse", baseline_correct=False),
            sample(subject_id=11, baseline_value="purchase", baseline_correct=True),
        ],
    )

    assert "intent" in metrics["label_metrics"]
    assert "intent" in metrics["baseline_label_metrics"]
    assert metrics["flip_total"] == 2
    flips = metrics["flips"]
    assert [flip["direction"] for flip in flips] == ["broken", "fixed"]
    assert flips[0]["subject_id"] == 11
    assert flips[0]["gold_value"] == "purchase"
    assert flips[0]["candidate_value"] == "browse"
    assert flips[1]["subject_id"] == 10
    assert flips[1]["candidate_value"] == "purchase"
    assert all("transcript" not in flip for flip in flips)


@pytest.mark.asyncio
async def test_default_envelope_rejects_a_prompt_that_grew() -> None:
    """The historical thresholds veto any candidate that spends more, however good."""

    from audio_graphy.services.tag_extractor import TagExtractorHarnessTrialExecutor

    executor = TagExtractorHarnessTrialExecutor(
        _grown_prompt_predictor(provider_calls=1),  # type: ignore[arg-type]
    )

    metrics = await executor.execute_trial(
        {"generation": {"prompt_template": "长得多的候选提示词", "max_tokens": 256}},
        [_grown_prompt_sample()],
    )

    assert metrics["cold_token_reduction"] < 0
    assert metrics["efficiency_gate_results"]["cold_token_reduction"] is False
    assert metrics["efficiency_gate_results"]["cold_cost_reduction"] is False
    assert metrics["efficiency_gate_passed"] is False
    assert metrics["efficiency_envelope"] == "token_reduction_v1"


@pytest.mark.asyncio
async def test_quality_uplift_envelope_admits_a_prompt_that_grew() -> None:
    """Opting into the uplift envelope lets a longer prompt be measured on quality."""

    from audio_graphy.services.tag_extractor import (
        QUALITY_UPLIFT_V1,
        TagExtractorHarnessTrialExecutor,
    )

    executor = TagExtractorHarnessTrialExecutor(
        _grown_prompt_predictor(provider_calls=1),  # type: ignore[arg-type]
        efficiency_envelope=QUALITY_UPLIFT_V1,
    )

    metrics = await executor.execute_trial(
        {"generation": {"prompt_template": "长得多的候选提示词", "max_tokens": 256}},
        [_grown_prompt_sample()],
    )

    assert metrics["cold_token_reduction"] < 0
    assert metrics["efficiency_gate_passed"] is True
    assert metrics["efficiency_envelope"] == "quality_uplift_v1"


@pytest.mark.asyncio
async def test_quality_uplift_envelope_still_refuses_extra_provider_calls() -> None:
    """Relaxing cost must not let a prompt grow until the input budget splits batches."""

    from audio_graphy.services.tag_extractor import (
        QUALITY_UPLIFT_V1,
        TagExtractorHarnessTrialExecutor,
    )

    executor = TagExtractorHarnessTrialExecutor(
        _grown_prompt_predictor(provider_calls=2),  # type: ignore[arg-type]
        efficiency_envelope=QUALITY_UPLIFT_V1,
    )

    metrics = await executor.execute_trial(
        {"generation": {"prompt_template": "长得多的候选提示词", "max_tokens": 256}},
        [_grown_prompt_sample()],
    )

    assert metrics["provider_call_delta"] > 0
    assert metrics["efficiency_gate_results"]["provider_calls_nonincrease"] is False
    assert metrics["efficiency_gate_passed"] is False


@pytest.mark.asyncio
async def test_tag_extractor_trial_executor_fails_closed_without_exact_review_ledger() -> None:
    from types import SimpleNamespace

    from audio_graphy.services.tag_extractor import TagExtractorHarnessTrialExecutor

    class Predictor:
        async def predict_materialized_frozen_input(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                assignments=(
                    {
                        "tag_key": "intent",
                        "tag_value": "purchase",
                        "confidence": 0.9,
                        "evidence_refs": [],
                    },
                ),
                review_items=(),
                latency_ms=10,
                provider_input_tokens=80,
                provider_output_tokens=20,
                reused_input_tokens=0,
                reused_output_tokens=0,
                provider_calls=1,
                cache_hits=0,
                strong_escalations=0,
                cost_microunits=15,
                counterfactual_saved_cost_microunits=0,
                unknown_billed_tokens=0,
            )

    executor = TagExtractorHarnessTrialExecutor(Predictor())  # type: ignore[arg-type]
    snapshot = {
        "subject_type": "dialogue_unit",
        "dialogue_unit_id": 10,
        "reception_id": 20,
        "schema_version_id": 30,
        "schema_checksum": "a" * 64,
        "scenario": "automotive",
        "transcript": "客户决定购买",
        "dialogue_unit_version": 1,
        "segments": [],
    }
    sample = {
        "tenant_id": "chang_an",
        "baseline_tagger_version_id": 40,
        "subject_type": "dialogue_unit",
        "subject_id": 10,
        "tag_key": "intent",
        "gold_value": "purchase",
        "truth_state": "present",
        "split": "validation",
        "is_critical": False,
        "baseline_predicted_value": "purchase",
        "baseline_is_correct": True,
        "input_snapshot": snapshot,
        "harness_execution_id": 50,
        "provider_cold_cost_microunits": 20,
        "provider_input_tokens": 110,
        "provider_output_tokens": 30,
        "reused_input_tokens": 0,
        "reused_output_tokens": 0,
        "provider_calls": 1,
        "cache_hits": 0,
        "unknown_billed_tokens": 0,
        # A legacy aggregate cannot reveal which target tag was reviewed.
        "review_item_count": 1,
        "provider_latency_ms": 30,
    }

    missing = await executor.execute_trial(
        {"output": {"thresholds": {"intent": 0.7}}},
        [sample],
    )
    assert missing["measurement_complete"] is False
    assert missing["efficiency_gate_results"]["measurement_complete"] is False
    assert missing["efficiency_gate_passed"] is False
    assert missing["feasible"] is False

    exact = await executor.execute_trial(
        {"output": {"thresholds": {"intent": 0.7}}},
        [{**sample, "baseline_reviewed": True, "review_item_count": 999}],
    )
    assert exact["measurement_complete"] is True
    assert exact["baseline_review_rate"] == 1
    assert exact["review_rate"] == 0
    assert exact["review_rate_delta"] == -1


@pytest.mark.asyncio
async def test_optimizer_reserves_aggregate_budget_before_trial_execution() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        execute_harness_trials,
    )

    class BudgetedExecutor:
        materialized_dimensions = frozenset({"output"})

        def __init__(self) -> None:
            self.called = False

        def estimate_trial_budget(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, int]:
            return {"provider_calls": 2, "provider_tokens": 100}

        async def execute_trial(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, Any]:
            self.called = True
            raise AssertionError("budget must fail before provider-backed execution")

    executor = BudgetedExecutor()
    with pytest.raises(GovernanceError, match="budget_exhausted: max_provider_calls"):
        await execute_harness_trials(
            baseline_config={},
            feedback_samples=[{"primary_failure_stage": "tag_reasoning"}],
            trial_executor=executor,
            budget={"max_provider_calls": 1},
        )

    assert executor.called is False


@pytest.mark.asyncio
async def test_optimizer_keeps_reservation_when_trial_measurement_is_incomplete() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        execute_harness_trials,
    )

    class IncompleteExecutor:
        materialized_dimensions = frozenset({"output"})

        def estimate_trial_budget(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, int]:
            return {
                "provider_calls": 2,
                "provider_tokens": 200,
                "cost_microunits": 40,
            }

        async def execute_trial(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
                "measurement_source": "tag_extractor_frozen_replay",
                "measurement_complete": False,
                "provider_input_tokens": 10,
                "provider_output_tokens": 5,
                "provider_calls": 1,
                "cost_microunits": 3,
                "unknown_billed_tokens": 128,
                "feasible": False,
            }

    reservations: list[dict[str, Any]] = []
    settlements: list[dict[str, int]] = []

    async def reserve(
        trial_index: int,
        mutation: str,
        candidate_checksum: str,
        estimate: Mapping[str, int | None],
    ) -> Mapping[str, Any]:
        reservation = {
            "id": "reservation-1",
            "trial_index": trial_index,
            "mutation": mutation,
            "candidate_checksum": candidate_checksum,
            **dict(estimate),
        }
        reservations.append(reservation)
        return reservation

    async def settle(
        _reservation: Mapping[str, Any],
        actual: Mapping[str, int],
    ) -> Mapping[str, Any]:
        settlements.append(dict(actual))
        return actual

    with pytest.raises(
        GovernanceError,
        match=r"measurement is incomplete.*reservation retained",
    ):
        await execute_harness_trials(
            baseline_config={},
            feedback_samples=[{"primary_failure_stage": "tag_reasoning"}],
            trial_executor=IncompleteExecutor(),
            max_candidates=1,
            budget={
                "max_provider_tokens": 1_000,
                "max_provider_calls": 10,
                "max_cost_microunits": 100,
            },
            reserve_budget=reserve,
            settle_budget=settle,
        )

    assert len(reservations) == 1
    assert settlements == []


@pytest.mark.asyncio
async def test_optimizer_budget_reservation_survives_crash_and_settles_actual_cost(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime, timedelta

    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TagOptimizationRun,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        TagGovernanceService,
    )

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    manifest_checksum = "9" * 64
    async with optimizer_factory() as session, session.begin():
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=seeded["baseline_id"],
            gold_set_version_id=seeded["gold_version_id"],
            dataset_snapshot_hash="8" * 64,
            trigger="manual",
            status="running",
            phase="search",
            cohort={"source": "durable-budget-test"},
            objective={"policy": "efficiency_guarded"},
            search_budget={
                "max_trials": 3,
                "sealed_holdout_queries": 1,
                "max_provider_tokens": 100,
                "max_provider_calls": 2,
                "max_cost_microunits": 50,
            },
            summary={"search_manifest_checksum": manifest_checksum},
            next_actions=[],
            artifacts=[],
            created_by=9,
        )
        session.add(run)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            origin="system",
            status="running",
            scope={"optimization_run_id": run.id},
            tagger_version_id=seeded["baseline_id"],
            idempotency_key=f"durable-budget:{run.id}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=7,
            lease_owner="optimizer-budget-worker",
            lease_token="optimizer-budget-lease",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            created_by=9,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        run_id = int(run.id)
        job_id = int(job.id)

    service = TagGovernanceService(optimizer_factory)
    common = {
        "tenant_id": "chang_an",
        "optimization_run_id": run_id,
        "optimization_job_id": job_id,
        "gold_set_version_id": seeded["gold_version_id"],
        "production_tagger_version_id": seeded["baseline_id"],
        "search_manifest_checksum": manifest_checksum,
        "lease_owner": "optimizer-budget-worker",
        "lease_token": "optimizer-budget-lease",
        "worker_id": "optimizer-budget-worker",
    }
    first = await service._reserve_optimization_trial_budget(
        **common,
        trial_index=0,
        mutation="baseline",
        candidate_checksum="a" * 64,
        estimate={
            "provider_tokens": 60,
            "provider_calls": 1,
            "cost_microunits": 30,
        },
    )

    # Simulate a process crash after Provider admission and before settlement.
    # The next reservation must conservatively consume the abandoned envelope.
    second = await service._reserve_optimization_trial_budget(
        **common,
        trial_index=1,
        mutation="generation.max_tokens=256",
        candidate_checksum="b" * 64,
        estimate={
            "provider_tokens": 30,
            "provider_calls": 1,
            "cost_microunits": 20,
        },
    )
    assert first["id"] != second["id"]
    aggregate = await service._settle_optimization_trial_budget(
        **common,
        reservation=second,
        actual={
            "provider_tokens": 25,
            "provider_calls": 1,
            "cost_microunits": 15,
        },
    )
    assert aggregate["provider_tokens"] == 85
    assert aggregate["provider_calls"] == 2
    assert aggregate["cost_microunits"] == 45

    with pytest.raises(
        GovernanceError,
        match="budget_exhausted: max_cost_microunits",
    ):
        await service._reserve_optimization_trial_budget(
            **common,
            trial_index=2,
            mutation="output.review_threshold=0.8",
            candidate_checksum="c" * 64,
            estimate={
                "provider_tokens": 0,
                "provider_calls": 0,
                "cost_microunits": 6,
            },
        )

    async with optimizer_factory() as session:
        persisted_run = await session.get(TagOptimizationRun, run_id)
        persisted_job = await session.get(TagExtractionJob, job_id)
        assert persisted_run is not None
        assert persisted_job is not None
        budget = persisted_run.search_budget
        assert budget["consumed_provider_tokens"] == 85
        assert budget["consumed_provider_calls"] == 2
        assert budget["consumed_cost_microunits"] == 45
        assert budget["reserved_provider_tokens"] == 0
        assert budget["reserved_provider_calls"] == 0
        assert budget["reserved_cost_microunits"] == 0
        assert budget["reservation"] is None
        assert budget["abandoned_reservation_count"] == 1
        assert budget["budget_exhausted_reason"] == "max_cost_microunits"
        assert persisted_job.revision == 7


@pytest.mark.asyncio
async def test_optimizer_rejects_search_when_no_measured_candidate_passes_gates() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        execute_harness_trials,
    )

    class InfeasibleExecutor:
        materialized_dimensions = frozenset({"output"})

        async def execute_trial(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
                "measurement_source": "test_frozen_replay",
                "measurement_complete": True,
                "provider_input_tokens": 0,
                "provider_output_tokens": 0,
                "provider_calls": 0,
                "cost_microunits": 0,
                "feasible": False,
                "quality_delta": 0.0,
                "review_rate_delta": 0.0,
                "p95_latency_delta": 0.0,
                "cost_delta": 0.0,
            }

    with pytest.raises(GovernanceError, match="no candidate"):
        await execute_harness_trials(
            baseline_config={},
            feedback_samples=[{"primary_failure_stage": "tag_reasoning"}],
            trial_executor=InfeasibleExecutor(),
            max_candidates=1,
        )


def test_bounded_search_validates_budget_and_rejects_client_error_samples() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        bounded_harness_search,
        reject_client_error_samples,
    )

    with pytest.raises(GovernanceError, match="between 1 and 32"):
        bounded_harness_search(
            baseline_config={},
            feedback_samples=[],
            evaluator=lambda _candidate, _samples: {"feasible": True},
            max_candidates=33,
        )
    with pytest.raises(GovernanceError, match="client-supplied error_samples"):
        reject_client_error_samples([{"gold_label_id": 1}])
    reject_client_error_samples(None)


def test_candidate_comparison_is_six_dimensional_and_deterministic() -> None:
    from audio_graphy.services.tag_governance import build_candidate_comparison

    comparison = build_candidate_comparison(
        left_trial_id=10,
        right_trial_id=11,
        left_spec={
            "context": {"neighbor_units": 0},
            "tools": {"primary_model": "weak"},
            "generation": {"temperature": 0},
            "orchestration": {"route": "weak_llm"},
            "memory": {"policy": "none"},
            "output": {"review_threshold": 0.7},
        },
        right_spec={
            "context": {"neighbor_units": 1},
            "tools": {"primary_model": "weak"},
            "generation": {"temperature": 0},
            "orchestration": {"route": "weak_llm"},
            "memory": {"policy": "none"},
            "output": {"review_threshold": 0.8},
        },
        left_metrics={"macro_f1": 0.80, "review_rate": 0.20},
        right_metrics={"macro_f1": 0.83, "review_rate": 0.18},
        left_reward={
            "feasible": True,
            "quality_delta": 0.0,
            "review_rate_delta": 0.0,
            "p95_latency_delta": 0.0,
            "cost_delta": 0.0,
        },
        right_reward={
            "feasible": True,
            "quality_delta": 0.03,
            "review_rate_delta": -0.02,
            "p95_latency_delta": 5.0,
            "cost_delta": 0.1,
        },
        left_badcase_count=20,
        right_badcase_count=13,
    )

    assert [item["dimension"] for item in comparison["dimensions"]] == [
        "context",
        "tools",
        "generation",
        "orchestration",
        "memory",
        "output",
    ]
    assert comparison["dimensions"][0] == {
        "dimension": "context",
        "before": {"neighbor_units": 0},
        "after": {"neighbor_units": 1},
    }
    assert comparison["metric_deltas"]["macro_f1"] == pytest.approx(0.03)
    assert comparison["metric_deltas"]["review_rate"] == pytest.approx(-0.02)
    assert comparison["improved_badcase_count"] == 7
    assert comparison["regressed_badcase_count"] == 0
    assert comparison["recommendation"]["trial_id"] == 11


@pytest.mark.asyncio
async def test_optimization_service_rejects_client_error_samples_before_database_access() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        TagGovernanceService,
    )

    service = TagGovernanceService(None)  # type: ignore[arg-type]
    with pytest.raises(GovernanceError, match="client-supplied error_samples"):
        await service.create_optimization_candidate(
            tenant_id="chang_an",
            gold_set_version_id=1,
            production_tagger_version_id=2,
            actor_user_id=3,
            error_samples=[],
        )


def test_sealed_holdout_allows_one_candidate_for_an_optimization_run() -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        enforce_sealed_holdout_access,
    )

    enforce_sealed_holdout_access(
        requested_candidate_id=22,
        requested_baseline_id=10,
        consumed_candidate_id=None,
        bound_baseline_id=10,
    )
    enforce_sealed_holdout_access(
        requested_candidate_id=22,
        requested_baseline_id=10,
        consumed_candidate_id=22,
        bound_baseline_id=10,
    )
    with pytest.raises(GovernanceConflictError, match="different candidate"):
        enforce_sealed_holdout_access(
            requested_candidate_id=23,
            requested_baseline_id=10,
            consumed_candidate_id=22,
            bound_baseline_id=10,
        )
    with pytest.raises(GovernanceConflictError, match="baseline"):
        enforce_sealed_holdout_access(
            requested_candidate_id=22,
            requested_baseline_id=11,
            consumed_candidate_id=22,
            bound_baseline_id=10,
        )


@pytest.mark.asyncio
async def test_get_harness_execution_returns_tenant_scoped_ordered_traces(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagHarnessExecution,
        TagHarnessStageTrace,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceNotFoundError,
        TagGovernanceService,
    )

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        execution = TagHarnessExecution(
            tenant_id="chang_an",
            tagger_version_id=999,
            subject_type="dialogue_unit",
            subject_id=77,
            input_hash="a" * 64,
            scene_profile={},
            resolved_harness_spec={},
            route="weak_llm",
            status="completed",
            output_snapshot={},
            started_at=now,
            finished_at=now,
        )
        session.add(execution)
        await session.flush()
        session.add_all(
            [
                TagHarnessStageTrace(
                    tenant_id="chang_an",
                    harness_execution_id=execution.id,
                    sequence_no=2,
                    stage="output",
                    status="completed",
                ),
                TagHarnessStageTrace(
                    tenant_id="chang_an",
                    harness_execution_id=execution.id,
                    sequence_no=1,
                    stage="context",
                    status="completed",
                ),
            ]
        )
        execution_id = execution.id

    service = TagGovernanceService(optimizer_factory)
    fetched, traces = await service.get_harness_execution(
        tenant_id="chang_an",
        harness_execution_id=execution_id,
    )

    assert fetched.id == execution_id
    assert [trace.sequence_no for trace in traces] == [1, 2]
    with pytest.raises(GovernanceNotFoundError):
        await service.get_harness_execution(
            tenant_id="other_tenant",
            harness_execution_id=execution_id,
        )


@pytest.mark.asyncio
async def test_compare_and_cancel_optimization_run_updates_durable_state(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TagOptimizationRun,
        TagOptimizationTrial,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            status="queued",
            scope={"optimization_run_id": 0},
            tagger_version_id=1,
            idempotency_key="compare-cancel-run",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=0,
            max_attempts=3,
            revision=1,
            created_by=7,
        )
        session.add(job)
        await session.flush()
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=1,
            gold_set_version_id=1,
            job_id=job.id,
            dataset_snapshot_hash="a" * 64,
            trigger="manual",
            status="queued",
            phase="prepare",
            cohort={"source": "eligible_feedback"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 2, "sealed_holdout_queries": 1},
            summary={},
            next_actions=["execute_bounded_search"],
            artifacts=[],
            created_by=7,
        )
        session.add(run)
        await session.flush()
        job.scope = {"optimization_run_id": run.id}
        left = TagOptimizationTrial(
            tenant_id="chang_an",
            optimization_run_id=run.id,
            ordinal=1,
            mutation={"description": "baseline"},
            harness_spec={"context": {"neighbor_units": 0}},
            status="pending",
            phase="train",
            reward_vector={
                "feasible": True,
                "quality_delta": 0,
                "review_rate_delta": 0,
                "p95_latency_delta": 0,
                "cost_delta": 0,
            },
            metrics={"macro_f1": 0.8},
            gate_results={},
            summary={},
            next_actions=[],
            artifacts=[],
        )
        right = TagOptimizationTrial(
            tenant_id="chang_an",
            optimization_run_id=run.id,
            ordinal=2,
            mutation={"description": "context.neighbor_units=1"},
            harness_spec={"context": {"neighbor_units": 1}},
            status="running",
            phase="validation",
            reward_vector={
                "feasible": True,
                "quality_delta": 0.03,
                "review_rate_delta": -0.02,
                "p95_latency_delta": 5,
                "cost_delta": 0.1,
            },
            metrics={"macro_f1": 0.83},
            gate_results={},
            summary={},
            next_actions=[],
            artifacts=[],
            started_at=now,
        )
        session.add_all([left, right])
        await session.flush()
        run_id = run.id
        job_id = job.id
        left_id = left.id
        right_id = right.id

    service = TagGovernanceService(optimizer_factory)
    comparison = await service.compare_optimization_trials(
        tenant_id="chang_an",
        optimization_run_id=run_id,
        left_trial_id=left_id,
        right_trial_id=right_id,
    )
    cancelled = await service.cancel_optimization_run(
        tenant_id="chang_an",
        optimization_run_id=run_id,
        actor_user_id=9,
    )

    assert comparison["recommendation"]["trial_id"] == right_id
    assert comparison["metric_deltas"]["macro_f1"] == pytest.approx(0.03)
    assert cancelled.status == "cancelled"
    async with optimizer_factory() as session:
        persisted_job = await session.get(TagExtractionJob, job_id)
        persisted_trials = list(
            (
                await session.execute(
                    select(TagOptimizationTrial)
                    .where(TagOptimizationTrial.optimization_run_id == run_id)
                    .order_by(TagOptimizationTrial.ordinal)
                )
            )
            .scalars()
            .all()
        )
    assert persisted_job is not None
    assert persisted_job.status == "cancelled"
    assert persisted_job.last_error_code == "OPTIMIZATION_CANCELLED"
    assert [trial.status for trial in persisted_trials] == ["cancelled", "cancelled"]
    assert all(trial.summary["cancelled"] is True for trial in persisted_trials)


@pytest.mark.parametrize(
    (
        "stage",
        "elapsed_hours",
        "served",
        "paired",
        "audited",
        "expected",
    ),
    [
        ("shadow", 25, 0, 0, 0, False),
        ("shadow", 23, 10_000, 500, 100, False),
        ("shadow", 24, 0, 500, 100, True),
        ("canary_5", 24, 1_000, 0, 200, True),
        ("canary_5", 24, 1_000, 0, 199, False),
        ("canary_25", 47, 10_000, 0, 1_000, False),
        ("canary_25", 48, 5_000, 0, 500, True),
    ],
)
def test_promotion_readiness_requires_time_and_stage_specific_trusted_support(
    stage: str,
    elapsed_hours: int,
    served: int,
    paired: int,
    audited: int,
    expected: bool,
) -> None:
    from datetime import timedelta

    from audio_graphy.services.tag_governance import evaluate_promotion_readiness

    readiness = evaluate_promotion_readiness(
        stage=stage,
        elapsed=timedelta(hours=elapsed_hours),
        served_count=served,
        paired_count=paired,
        audited_count=audited,
    )

    assert readiness.passed is expected
    assert readiness.requirements["duration_hours"] in {24, 48}


@pytest.mark.asyncio
async def test_create_optimization_run_server_binds_production_and_materializes_trials(
    optimizer_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TagExtractionJob,
        TagFeedbackEvent,
        TaggerVersion,
        TagGoldSet,
        TagGoldSetVersion,
        TagOptimizationRun,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="optimizer",
            name="Optimizer",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["purchase", "none"],
                    "critical_values": ["purchase"],
                    "negative_values": ["none"],
                    "subject_types": ["dialogue_unit"],
                }
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(schema_version)
        await session.flush()
        old = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="old",
            engine="hybrid",
            prompt_content="old",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
        )
        production = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="production",
            engine="hybrid",
            prompt_content="production",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={"intent": 0.7},
            harness_spec={
                "context": {"neighbor_units": 0},
                "memory": {"example_count": 0, "strategy": "similar"},
                "orchestration": {
                    "route": "rule_llm_fusion",
                    "fusion": "score_priority",
                },
                "output": {"threshold_offset": 0.0},
            },
            config_checksum="c" * 64,
            status="qualified",
            created_by=1,
        )
        session.add_all([old, production])
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="complete-gold",
            name="完整金标",
            schema_version_id=schema_version.id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        gold_version = TagGoldSetVersion(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="2026-07",
            status="frozen",
            checksum="d" * 64,
            dataset_snapshot_hash="e" * 64,
            completeness_manifest={"complete": True, "legacy_sparse": False},
            item_count=200,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(gold_version)
        await session.flush()
        session.add_all(
            _gold_lane_labels(
                gold_set_version_id=gold_version.id,
                subject_ids=(10_001, 10_002, 10_003, 10_004),
            )
        )
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=production.id,
            baseline_tagger_version_id=old.id,
            gold_set_version_id=gold_version.id,
            evaluator_version="tag-evaluator-v2",
            dataset_snapshot_hash="e" * 64,
            status="completed",
            metrics={},
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        session.add(
            TagDeployment(
                tenant_id="chang_an",
                tagger_version_id=production.id,
                evaluation_run_id=evaluation.id,
                baseline_tagger_version_id=old.id,
                status="production",
                traffic_percent=100,
                revision=1,
                created_by=1,
                approved_by=1,
                approved_at=now,
            )
        )
        session.add_all(
            [
                TagFeedbackEvent(
                    tenant_id="chang_an",
                    source="human",
                    truth_tier="t2",
                    subject_type="dialogue_unit",
                    subject_id=index + 1,
                    tag_key="intent",
                    truth_state="present",
                    error_stage="tag_reasoning",
                    correction={"action": "correct"},
                    payload={},
                    training_eligible=True,
                    occurred_at=now,
                )
                for index in range(200)
            ]
        )
        await session.flush()
        gold_version_id = gold_version.id
        production_id = production.id

    service = TagGovernanceService(optimizer_factory)
    created = await service.create_optimization_run(
        tenant_id="chang_an",
        gold_set_version_id=gold_version_id,
        cohort={"source": "eligible_feedback"},
        objective={"policy": "balanced"},
        search_budget={"max_trials": 24, "sealed_holdout_queries": 1},
        trigger="manual",
        actor_user_id=9,
    )
    listed = await service.list_optimization_runs(tenant_id="chang_an")
    fetched, trials = await service.get_optimization_run(
        tenant_id="chang_an",
        optimization_run_id=created.id,
    )
    overview = await service.get_evolution_overview(tenant_id="chang_an")

    assert created.baseline_tagger_version_id == production_id
    assert created.dataset_snapshot_hash == "e" * 64
    assert created.sealed_release_key is not None
    assert created.job_id is not None
    assert created.summary["new_feedback_count"] == 200
    assert created.summary["feedback_by_tag"] == {"intent": 200}
    assert created.summary["coverage_gate_passed"] is True
    async with optimizer_factory() as session:
        job = await session.get(TagExtractionJob, created.job_id)
        assert job is not None
        assert job.job_type == "optimize"
        assert job.status == "queued"
        assert job.scope == {"optimization_run_id": created.id}
        assert job.total_items == 1
    assert 1 < len(trials) <= 24
    assert [trial.ordinal for trial in trials] == list(range(1, len(trials) + 1))
    assert fetched.id == created.id
    assert [item.id for item in listed] == [created.id]
    assert overview["production_harness"]["id"] == production_id
    assert overview["recommended_gold_set_version_id"] == gold_version_id
    assert "完整金标" in overview["recommended_gold_set_label"]

    async def create_worker_candidate(**kwargs: Any) -> tuple[Any, dict[str, Any]]:
        from audio_graphy.models.tag_governance import (
            TaggerVersion,
            TagOptimizationTrial,
        )
        from audio_graphy.services.tag_governance import canonical_checksum

        async with optimizer_factory() as session, session.begin():
            first_trial = (
                await session.execute(
                    select(TagOptimizationTrial)
                    .where(TagOptimizationTrial.optimization_run_id == created.id)
                    .order_by(TagOptimizationTrial.ordinal)
                    .limit(1)
                )
            ).scalar_one()
            candidate = TaggerVersion(
                tenant_id="chang_an",
                schema_version_id=schema_version.id,
                version="worker-candidate",
                engine="hybrid",
                prompt_content="worker candidate",
                rule_bundle={"dsl_version": "1", "rules": []},
                model_version="weak",
                thresholds={"intent": 0.7},
                harness_spec=first_trial.harness_spec,
                parent_version_id=production_id,
                origin="optimizer",
                optimization_run_id=created.id,
                config_checksum=canonical_checksum(
                    {
                        "optimization_run_id": created.id,
                        "harness_spec": first_trial.harness_spec,
                    }
                ),
                status="draft",
                created_by=9,
            )
            session.add(candidate)
            await session.flush()
            return candidate, {
                "bounded_search": {
                    "winner": {"index": 0},
                    "trials": [
                        {
                            "index": 0,
                            "mutation": "baseline",
                            "reward": {
                                "feasible": True,
                                "quality_delta": 0.02,
                                "review_rate_delta": -0.01,
                                "p95_latency_delta": 0,
                                "cost_delta": 0,
                            },
                            "metrics": {"macro_f1": 0.84},
                        }
                    ],
                }
            }

    monkeypatch.setattr(
        service,
        "create_optimization_candidate",
        create_worker_candidate,
    )
    enqueued_evaluation: dict[str, Any] = {}

    async def enqueue_sealed_holdout(
        _self: Any,
        **kwargs: Any,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        enqueued_evaluation.update(kwargs)
        return SimpleNamespace(id=701), SimpleNamespace(id=702)

    from audio_graphy.services.tag_evaluator import TagEvaluationService

    monkeypatch.setattr(TagEvaluationService, "enqueue", enqueue_sealed_holdout)
    winner = await service.execute_optimization_run(
        tenant_id="chang_an",
        optimization_run_id=created.id,
        actor_user_id=9,
    )
    completed, completed_trials = await service.get_optimization_run(
        tenant_id="chang_an",
        optimization_run_id=created.id,
    )

    assert winner.optimization_run_id == created.id
    assert completed.status == "running"
    assert completed.phase == "holdout"
    assert completed.winner_tagger_version_id is None
    assert completed.candidate_tagger_version_id == winner.id
    assert completed.summary["worker_completed"] is True
    assert completed.summary["evaluation_run_id"] == 701
    assert completed.summary["evaluation_job_id"] == 702
    comparison = completed.summary["candidate_comparison"]
    assert len(comparison["dimensions"]) == 6
    assert comparison["recommendation"]["trial_id"] == completed_trials[0].id
    assert completed.next_actions == ["await_sealed_holdout_evaluation"]
    assert enqueued_evaluation == {
        "tenant_id": "chang_an",
        "tagger_version_id": winner.id,
        "gold_set_version_id": gold_version_id,
        "baseline_tagger_version_id": production_id,
        "idempotency_key": f"optimization-run:{created.id}:sealed-holdout",
        "actor_user_id": 9,
        "evaluation_lane": "holdout",
        "release_service": True,
        "trusted_optimization_binding": True,
    }
    assert completed_trials[0].status == "completed"
    assert completed_trials[0].candidate_tagger_version_id == winner.id
    assert all(trial.status in {"completed", "pruned"} for trial in completed_trials)

    with pytest.raises(GovernanceConflictError) as duplicate_exc:
        await service.create_server_bound_optimization_run(
            tenant_id="chang_an",
            cohort={"source": "manual_recheck"},
            target_policy={"policy": "balanced"},
            search_budget={"max_trials": 24, "sealed_holdout_queries": 1},
            actor_user_id=9,
        )
    assert str(duplicate_exc.value) == "gold_not_release_ready"

    async with optimizer_factory() as session, session.begin():
        persisted_created = await session.get(TagOptimizationRun, created.id)
        assert persisted_created is not None
        persisted_created.status = "completed"
        persisted_created.phase = "completed"
        persisted_created.finished_at = now

    first_weekly = await service.run_weekly_optimization_checks(
        at=now,
        actor_user_id=0,
    )
    repeated_weekly = await service.run_weekly_optimization_checks(
        at=now,
        actor_user_id=0,
    )

    assert first_weekly == []
    assert repeated_weekly == []


@pytest.mark.asyncio
async def test_feedback_coverage_requires_thirty_samples_for_every_affected_tag(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import TagFeedbackEvent
    from audio_graphy.services.tag_governance import TagGovernanceService

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        session.add_all(
            [
                TagFeedbackEvent(
                    tenant_id="chang_an",
                    source="human",
                    truth_tier="t2",
                    subject_type="dialogue_unit",
                    subject_id=index + 1,
                    tag_key=tag_key,
                    truth_state="present",
                    error_stage="tag_reasoning",
                    correction={"action": "correct"},
                    payload={},
                    training_eligible=True,
                    occurred_at=now,
                )
                for tag_key, support in (("intent", 200), ("risk", 29))
                for index in range(support)
            ]
        )

    service = TagGovernanceService(optimizer_factory)
    async with optimizer_factory() as session:
        insufficient = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort={"source": "coverage-test"},
        )
    assert insufficient.total == 229
    assert insufficient.passed is False
    assert insufficient.blockers == ("tag_support_below_30:risk",)

    async with optimizer_factory() as session, session.begin():
        session.add(
            TagFeedbackEvent(
                tenant_id="chang_an",
                source="human",
                truth_tier="t2",
                subject_type="dialogue_unit",
                subject_id=999,
                tag_key="risk",
                truth_state="absent",
                error_stage="tag_reasoning",
                correction={"action": "reject"},
                payload={},
                training_eligible=True,
                occurred_at=now,
            )
        )
    async with optimizer_factory() as session:
        sufficient = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort={"source": "coverage-test"},
        )

    assert sufficient.total == 230
    assert sufficient.by_tag == {"intent": 200, "risk": 30}
    assert sufficient.passed is True


@pytest.mark.asyncio
async def test_feedback_coverage_fails_when_schema_supported_subject_domain_is_missing(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import TagFeedbackEvent
    from audio_graphy.services.tag_governance import TagGovernanceService

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        session.add_all(
            [
                TagFeedbackEvent(
                    tenant_id="chang_an",
                    source="human",
                    truth_tier="t2",
                    subject_type="dialogue_unit",
                    subject_id=index + 1,
                    tag_key="intent",
                    truth_state="present",
                    error_stage="tag_reasoning",
                    correction={"action": "correct"},
                    payload={},
                    training_eligible=True,
                    occurred_at=now,
                )
                for index in range(200)
            ]
        )

    service = TagGovernanceService(optimizer_factory)
    async with optimizer_factory() as session:
        coverage = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort={"source": "missing-domain-test"},
            schema_definitions=[
                {
                    "key": "intent",
                    "subject_types": ["dialogue_unit", "reception"],
                }
            ],
        )

    assert coverage.total == 200
    assert coverage.by_subject_tag == {
        "dialogue_unit:intent": 200,
        "reception:intent": 0,
    }
    assert coverage.passed is False
    assert coverage.blockers == ("tag_support_below_30:reception:intent",)


async def _seed_cohort_optimizer_context(
    optimizer_factory: async_sessionmaker[AsyncSession],
    *,
    missing_gold_lane: str | None = None,
    complete_schema_holdout: bool = True,
    critical_intent: bool = False,
    critical_positive_support: int = 73,
    intent_scenarios: tuple[str, ...] = (),
    required_intent: bool = False,
    intent_absent_support: int = 30,
) -> dict[str, int]:
    from datetime import UTC, datetime, timedelta

    from audio_graphy.models.reception import DialogueUnit, Reception
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
        TagGoldLabel,
        TagGoldSet,
        TagGoldSetVersion,
        TagSchema,
        TagSchemaVersion,
    )

    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="cohort-optimizer",
            name="Cohort optimizer",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["purchase", "none"],
                    "subject_types": (
                        ["dialogue_unit"]
                        if critical_intent or required_intent
                        else ["dialogue_unit", "reception"]
                    ),
                    "scenarios": list(intent_scenarios),
                    "critical": critical_intent,
                    "negative_values": (["none"] if critical_intent else []),
                    "required": required_intent,
                },
                {
                    "key": "objection",
                    "value_type": "enum",
                    "allowed_values": ["price", "none"],
                    "subject_types": ["dialogue_unit", "reception"],
                },
            ],
            checksum="1" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(schema_version)
        await session.flush()
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="baseline",
            engine="hybrid",
            prompt_content="baseline",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={"intent": 0.7, "objection": 0.7},
            harness_spec={
                "context": {"neighbor_units": 0},
                "memory": {"example_count": 0, "strategy": "similar"},
                "orchestration": {
                    "route": "rule_llm_fusion",
                    "fusion": "score_priority",
                },
                "output": {"threshold_offset": 0.0},
            },
            config_checksum="2" * 64,
            status="qualified",
            created_by=1,
        )
        session.add(baseline)
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="cohort-gold",
            name="Cohort gold",
            schema_version_id=schema_version.id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        gold_version = TagGoldSetVersion(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="2026-07",
            status="frozen",
            checksum="3" * 64,
            dataset_snapshot_hash="4" * 64,
            completeness_manifest={"complete": True, "legacy_sparse": False},
            item_count=0,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(gold_version)
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=baseline.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=gold_version.id,
            evaluator_version="tag-evaluator-v2",
            dataset_snapshot_hash="4" * 64,
            status="completed",
            metrics={},
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        session.add(
            TagDeployment(
                tenant_id="chang_an",
                tagger_version_id=baseline.id,
                evaluation_run_id=evaluation.id,
                baseline_tagger_version_id=baseline.id,
                status="production",
                traffic_percent=100,
                revision=1,
                created_by=1,
                approved_by=1,
                approved_at=now,
            )
        )
        target_reception = Reception(
            tenant_id="chang_an",
            external_session_id="target",
            scenario="automotive",
            store_id="S1",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(minutes=10),
        )
        wrong_store_reception = Reception(
            tenant_id="chang_an",
            external_session_id="wrong-store",
            scenario="automotive",
            store_id="S2",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(minutes=10),
        )
        wrong_scenario_reception = Reception(
            tenant_id="chang_an",
            external_session_id="wrong-scenario",
            scenario="gold",
            store_id="S1",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(minutes=10),
        )
        session.add_all(
            [
                target_reception,
                wrong_store_reception,
                wrong_scenario_reception,
            ]
        )
        await session.flush()
        target_train_unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=target_reception.id,
            unit_index=0,
            start_sec=0,
            end_sec=10,
        )
        target_validation_unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=target_reception.id,
            unit_index=1,
            start_sec=10,
            end_sec=20,
        )
        wrong_store_unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=wrong_store_reception.id,
            unit_index=0,
            start_sec=0,
            end_sec=10,
        )
        wrong_scenario_unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=wrong_scenario_reception.id,
            unit_index=0,
            start_sec=0,
            end_sec=10,
        )
        preflight_units = [
            DialogueUnit(
                tenant_id="chang_an",
                reception_id=target_reception.id,
                unit_index=index,
                start_sec=float(index * 10),
                end_sec=float((index + 1) * 10),
            )
            for index in range(2, 6)
        ]
        session.add_all(
            [
                target_train_unit,
                target_validation_unit,
                wrong_store_unit,
                wrong_scenario_unit,
                *preflight_units,
            ]
        )
        await session.flush()
        preflight_labels = _gold_lane_labels(
            gold_set_version_id=gold_version.id,
            subject_ids=tuple(unit.id for unit in preflight_units),  # type: ignore[arg-type]
            reception_id=target_reception.id,
            missing_lane=missing_gold_lane,
            review_decision_base=2_000_000,
            positive_support=critical_positive_support,
            absent_support=intent_absent_support,
        )
        for label in preflight_labels:
            if label.split == "holdout":
                # The sealed release lane deliberately sits outside the
                # client-selected S1 cohort; create must never apply client
                # filters to global release-readiness.
                label.reception_id = wrong_store_reception.id
        extra_holdout_labels: list[TagGoldLabel] = []
        if complete_schema_holdout:
            dialogue_subject_ids = [int(preflight_units[2].id) + index for index in range(30)]
            reception_subject_ids = [10_000_000 + index for index in range(30)]
            extra_pairs = [
                (
                    "dialogue_unit",
                    subject_id,
                    int(target_reception.id),
                    "objection",
                    "price",
                )
                for subject_id in dialogue_subject_ids
            ]
            extra_pairs.extend(
                (subject_type, subject_id, subject_id, tag_key, positive_value)
                for subject_type, tag_key, positive_value in (
                    ("reception", "intent", "purchase"),
                    ("reception", "objection", "price"),
                )
                for subject_id in reception_subject_ids
            )
            for index, (
                subject_type,
                subject_id,
                reception_id,
                tag_key,
                positive_value,
            ) in enumerate(extra_pairs, start=1):
                positive = (index - 1) % 30 < 15
                lane = "holdout_t3_present" if positive else "holdout_t3_absent"
                if lane == missing_gold_lane:
                    continue
                extra_holdout_labels.append(
                    TagGoldLabel(
                        tenant_id="chang_an",
                        gold_set_version_id=gold_version.id,
                        review_decision_id=3_000_000 + index,
                        reception_id=reception_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        tag_key=tag_key,
                        tag_value=(positive_value if positive else None),
                        evidence_refs=[],
                        truth_state=("present" if positive else "absent"),
                        truth_tier="t3",
                        input_hash=f"{3_000_000 + index:064x}",
                        input_snapshot={},
                        annotation_quality={},
                        cohort="optimizer-schema-support",
                        completeness_manifest={"complete": True},
                        split="holdout",
                    )
                )
        all_preflight_labels = [*preflight_labels, *extra_holdout_labels]
        session.add_all(all_preflight_labels)
        gold_version.item_count = len(all_preflight_labels)
        return {
            "schema_version_id": schema_version.id,
            "baseline_id": baseline.id,
            "gold_version_id": gold_version.id,
            "target_reception_id": target_reception.id,
            "wrong_store_reception_id": wrong_store_reception.id,
            "wrong_scenario_reception_id": wrong_scenario_reception.id,
            "target_train_unit_id": target_train_unit.id,
            "target_validation_unit_id": target_validation_unit.id,
            "wrong_store_unit_id": wrong_store_unit.id,
            "wrong_scenario_unit_id": wrong_scenario_unit.id,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_lane",
    [
        "train",
        "validation",
        "holdout_t3_present",
        "holdout_t3_absent",
    ],
)
async def test_create_optimization_run_rejects_gold_missing_required_lane(
    optimizer_factory: async_sessionmaker[AsyncSession],
    missing_lane: str,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TagOptimizationRun,
        TagOptimizationTrial,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    seeded = await _seed_cohort_optimizer_context(
        optimizer_factory,
        missing_gold_lane=missing_lane,
    )
    service = TagGovernanceService(optimizer_factory)

    expected_error = (
        "gold_not_optimization_ready"
        if missing_lane in {"train", "validation"}
        else "gold_not_release_ready"
    )
    with pytest.raises(GovernanceConflictError) as exc_info:
        await service.create_optimization_run(
            tenant_id="chang_an",
            gold_set_version_id=seeded["gold_version_id"],
            cohort={"source": "gold-preflight-test"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
            trigger="manual",
            actor_user_id=9,
        )

    assert str(exc_info.value) == expected_error
    assert missing_lane not in str(exc_info.value)
    async with optimizer_factory() as session:
        assert list((await session.execute(select(TagOptimizationRun))).scalars()) == []
        assert (
            list(
                (
                    await session.execute(
                        select(TagExtractionJob).where(TagExtractionJob.job_type == "optimize")
                    )
                ).scalars()
            )
            == []
        )
        assert list((await session.execute(select(TagOptimizationTrial))).scalars()) == []


@pytest.mark.asyncio
async def test_create_optimization_run_rejects_missing_schema_subject_tag_holdout_support(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    seeded = await _seed_cohort_optimizer_context(
        optimizer_factory,
        complete_schema_holdout=False,
    )
    service = TagGovernanceService(optimizer_factory)

    with pytest.raises(GovernanceConflictError) as exc_info:
        await service.create_optimization_run(
            tenant_id="chang_an",
            gold_set_version_id=seeded["gold_version_id"],
            cohort={"source": "missing-schema-domain-holdout"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
            trigger="manual",
            actor_user_id=9,
        )

    assert str(exc_info.value) == "gold_not_release_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "run_phase", "job_status", "error"),
    [
        ("cancelled", "search", "cancelled", "optimization run is not active"),
        ("running", "holdout", "running", "optimization run is not active"),
        ("running", "search", "failed", "optimization job is not active"),
    ],
)
async def test_candidate_requires_an_active_optimization_run_and_job(
    optimizer_factory: async_sessionmaker[AsyncSession],
    run_status: str,
    run_phase: str,
    job_status: str,
    error: str,
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TaggerVersion,
        TagOptimizationRun,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    now = datetime.now(UTC)
    async with optimizer_factory() as session, session.begin():
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=seeded["baseline_id"],
            gold_set_version_id=seeded["gold_version_id"],
            dataset_snapshot_hash="4" * 64,
            trigger="manual",
            status=run_status,
            phase=run_phase,
            cohort={"source": "active-state-test"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
            summary={},
            next_actions=[],
            artifacts=[],
            created_by=9,
            finished_at=(now if run_status == "cancelled" else None),
        )
        session.add(run)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            origin="system",
            status=job_status,
            scope={"optimization_run_id": run.id},
            tagger_version_id=seeded["baseline_id"],
            idempotency_key=f"active-state:{run_status}:{run_phase}:{job_status}",
            total_items=1,
            completed_items=0,
            failed_items=1 if job_status == "failed" else 0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=1,
            created_by=9,
            finished_at=(now if job_status in {"failed", "cancelled"} else None),
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        run_id = run.id

    service = TagGovernanceService(optimizer_factory)
    with pytest.raises(GovernanceConflictError, match=error):
        await service.create_optimization_candidate(
            tenant_id="chang_an",
            gold_set_version_id=seeded["gold_version_id"],
            production_tagger_version_id=seeded["baseline_id"],
            actor_user_id=9,
            optimization_run_id=run_id,
        )

    async with optimizer_factory() as session:
        candidates = list(
            (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.optimization_run_id == run_id,
                    )
                )
            ).scalars()
        )
    assert candidates == []


@pytest.mark.asyncio
async def test_cancelling_optimization_rejects_an_unattached_draft_candidate(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TaggerVersion,
        TagOptimizationRun,
    )
    from audio_graphy.services.tag_governance import (
        TagGovernanceService,
        canonical_checksum,
    )

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    async with optimizer_factory() as session, session.begin():
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=seeded["baseline_id"],
            gold_set_version_id=seeded["gold_version_id"],
            dataset_snapshot_hash="4" * 64,
            trigger="manual",
            status="running",
            phase="search",
            cohort={"source": "cancel-race-test"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
            summary={},
            next_actions=["create_candidate_version"],
            artifacts=[],
            created_by=9,
        )
        session.add(run)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            origin="system",
            status="running",
            scope={"optimization_run_id": run.id},
            tagger_version_id=seeded["baseline_id"],
            idempotency_key=f"cancel-race:{run.id}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="optimizer-worker",
            created_by=9,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=seeded["schema_version_id"],
            version=f"cancel-race-{run.id}",
            engine="hybrid",
            prompt_content="candidate",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={"intent": 0.7},
            parent_version_id=seeded["baseline_id"],
            origin="optimizer",
            optimization_run_id=run.id,
            config_checksum=canonical_checksum({"optimization_run_id": run.id}),
            status="draft",
            created_by=9,
        )
        session.add(candidate)
        await session.flush()
        run_id = run.id
        candidate_id = candidate.id

    service = TagGovernanceService(optimizer_factory)
    await service.cancel_optimization_run(
        tenant_id="chang_an",
        optimization_run_id=run_id,
        actor_user_id=10,
    )

    async with optimizer_factory() as session:
        persisted_candidate = await session.get(TaggerVersion, candidate_id)
        drafts = list(
            (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.optimization_run_id == run_id,
                        TaggerVersion.status == "draft",
                    )
                )
            ).scalars()
        )
    assert persisted_candidate is not None
    assert persisted_candidate.status == "rejected"
    assert drafts == []


@pytest.mark.asyncio
async def test_feedback_coverage_pushes_down_filters_and_deduplicates_subject_labels(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import TagFeedbackEvent
    from audio_graphy.services.tag_governance import TagGovernanceService

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    now = datetime.now(UTC)
    event_groups = (
        (seeded["target_train_unit_id"], "intent"),
        (seeded["target_train_unit_id"], "objection"),
        (seeded["wrong_store_unit_id"], "intent"),
        (seeded["wrong_scenario_unit_id"], "intent"),
    )
    async with optimizer_factory() as session, session.begin():
        session.add_all(
            [
                TagFeedbackEvent(
                    tenant_id="chang_an",
                    source="human",
                    truth_tier="t2",
                    subject_type="dialogue_unit",
                    subject_id=subject_id,
                    tag_key=tag_key,
                    truth_state="present",
                    error_stage="tag_reasoning",
                    correction={"action": "correct"},
                    payload={},
                    training_eligible=True,
                    occurred_at=now,
                )
                for subject_id, tag_key in event_groups
                for _index in range(35)
            ]
        )

    service = TagGovernanceService(optimizer_factory)
    async with optimizer_factory() as session:
        coverage = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort={
                "source": "tag_insights",
                "filters": {
                    "store_ids": ["S1"],
                    "scenarios": ["automotive"],
                    "reception_ids": [seeded["target_reception_id"]],
                    "label_keys": ["intent"],
                },
            },
        )

    assert coverage.total == 1
    assert coverage.by_tag == {"intent": 1}


@pytest.mark.asyncio
async def test_feedback_watermark_is_scoped_to_the_same_semantic_cohort(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TagFeedbackEvent,
        TagOptimizationRun,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    now = datetime.now(UTC)
    intent_cohort = {
        "source": "tag_insights",
        "filters": {"label_keys": ["intent"]},
        "group_ids": [],
        "conflict_only": False,
    }
    objection_cohort = {
        "source": "tag_insights",
        "filters": {"label_keys": ["objection"]},
        "group_ids": [],
        "conflict_only": False,
    }
    async with optimizer_factory() as session, session.begin():
        older_objection = TagFeedbackEvent(
            tenant_id="chang_an",
            source="human",
            truth_tier="t2",
            subject_type="dialogue_unit",
            subject_id=seeded["target_train_unit_id"],
            tag_key="objection",
            truth_state="present",
            error_stage="tag_reasoning",
            correction={"action": "correct"},
            payload={},
            training_eligible=True,
            occurred_at=now,
        )
        newer_intent = TagFeedbackEvent(
            tenant_id="chang_an",
            source="human",
            truth_tier="t2",
            subject_type="dialogue_unit",
            subject_id=seeded["target_train_unit_id"],
            tag_key="intent",
            truth_state="present",
            error_stage="tag_reasoning",
            correction={"action": "correct"},
            payload={},
            training_eligible=True,
            occurred_at=now,
        )
        session.add_all([older_objection, newer_intent])
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            status="completed",
            scope={"optimization_run_id": 1},
            tagger_version_id=seeded["baseline_id"],
            idempotency_key="cohort-watermark-intent",
            total_items=1,
            completed_items=1,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=2,
            created_by=1,
            finished_at=now,
        )
        session.add(job)
        await session.flush()
        session.add(
            TagOptimizationRun(
                tenant_id="chang_an",
                baseline_tagger_version_id=seeded["baseline_id"],
                gold_set_version_id=seeded["gold_version_id"],
                dataset_snapshot_hash="4" * 64,
                trigger="manual",
                status="completed",
                phase="completed",
                cohort=intent_cohort,
                objective={"policy": "balanced"},
                search_budget={"max_trials": 1, "sealed_holdout_queries": 1},
                summary={
                    "feedback_cohort_key": (
                        TagGovernanceService._optimization_feedback_cohort_key(intent_cohort)
                    ),
                    "feedback_watermark_event_id": newer_intent.id,
                },
                job_id=job.id,
                created_by=1,
                finished_at=now,
            )
        )
        objection_event_id = int(older_objection.id)
        intent_event_id = int(newer_intent.id)

    service = TagGovernanceService(optimizer_factory)
    async with optimizer_factory() as session:
        objection_coverage = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort=objection_cohort,
        )
        intent_coverage = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort=intent_cohort,
        )

    assert objection_event_id < intent_event_id
    assert objection_coverage.after_event_id == 0
    assert objection_coverage.by_tag == {"objection": 1}
    assert intent_coverage.after_event_id == intent_event_id
    assert intent_coverage.by_tag == {"intent": 0}


@pytest.mark.asyncio
async def test_optimization_run_freezes_resolved_reception_ids_and_checksum(
    optimizer_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        TagGovernanceService,
        canonical_checksum,
    )

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    service = TagGovernanceService(optimizer_factory)
    created = await service.create_optimization_run(
        tenant_id="chang_an",
        gold_set_version_id=seeded["gold_version_id"],
        cohort={
            "source": "tag_insights",
            "filters": {
                "store_ids": ["S1"],
                "scenarios": ["automotive"],
                "reception_ids": [
                    seeded["target_reception_id"],
                    seeded["wrong_store_reception_id"],
                    seeded["wrong_scenario_reception_id"],
                ],
                "label_keys": ["intent"],
            },
        },
        objective={"policy": "balanced"},
        search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
        trigger="manual",
        actor_user_id=9,
    )

    resolved_reception_ids = [seeded["target_reception_id"]]
    assert created.cohort["resolved_reception_ids"] == resolved_reception_ids
    assert created.cohort["resolved_reception_checksum"] == canonical_checksum(
        {"resolved_reception_ids": resolved_reception_ids}
    )
    assert created.summary["gold_preflight_passed"] is True
    assert "gold_eligible_label_count" not in created.summary
    assert "gold_holdout_support_by_subject_tag" not in created.summary


async def _add_optimizer_gold_sample(
    session: AsyncSession,
    *,
    seeded: dict[str, int],
    subject_type: str,
    subject_id: int,
    reception_id: int,
    tag_key: str,
    predicted_value: str,
    gold_value: str | None,
    truth_state: str,
    split: str,
    score: float,
    suffix: str,
    resulting_fact: bool,
    baseline_review_tag_keys: tuple[str, ...] | None = None,
) -> None:
    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagAssignmentFact,
        TagGoldLabel,
        TagHarnessExecution,
        TagReviewDecision,
        TagReviewTask,
    )

    now = datetime.now(UTC)
    proposed = TagAssignmentFact(
        tenant_id="chang_an",
        subject_type=subject_type,
        subject_id=subject_id,
        reception_id=reception_id,
        dialogue_unit_id=(subject_id if subject_type == "dialogue_unit" else None),
        tag_key=tag_key,
        tag_value=predicted_value,
        confidence=score,
        evidence_refs=[
            {
                "start_sec": 0,
                "end_sec": 1,
                "text_excerpt": f"evidence-{suffix}",
            }
        ],
        source="llm",
        schema_version_id=seeded["schema_version_id"],
        tagger_version_id=seeded["baseline_id"],
        input_hash=(suffix[0] * 64),
        recipe_hash=(suffix[-1] * 64),
        revision=1,
        tombstone=False,
        assigned_at=now,
    )
    session.add(proposed)
    await session.flush()
    harness_execution_id: int | None = None
    if baseline_review_tag_keys is not None:
        execution = TagHarnessExecution(
            tenant_id="chang_an",
            tagger_version_id=seeded["baseline_id"],
            subject_type=subject_type,
            subject_id=subject_id,
            input_hash=(suffix[0] * 64),
            scene_profile={},
            resolved_harness_spec={},
            route="weak_llm",
            status="completed",
            output_snapshot={
                "review_item_count": len(baseline_review_tag_keys),
                "review_items": [
                    {"tag_key": review_tag_key} for review_tag_key in baseline_review_tag_keys
                ],
                "usage": {
                    "provider_input_tokens": 10,
                    "provider_output_tokens": 10,
                    "reused_input_tokens": 0,
                    "reused_output_tokens": 0,
                    "provider_calls": 1,
                    "cache_hits": 0,
                    "cost_microunits": 10,
                    "cold_cache_cost_microunits": 10,
                    "unknown_billed_tokens": 0,
                },
            },
            latency_ms=10,
            token_count=20,
            cost_units=0.01,
            started_at=now,
            finished_at=now,
        )
        session.add(execution)
        await session.flush()
        harness_execution_id = int(execution.id)
    task = TagReviewTask(
        tenant_id="chang_an",
        batch_id=f"batch-{suffix}",
        subject_type=subject_type,
        subject_id=subject_id,
        reception_id=reception_id,
        tag_key=tag_key,
        proposed_value=predicted_value,
        confidence=score,
        evidence_refs=proposed.evidence_refs,
        proposed_fact_id=proposed.id,
        source_harness_execution_id=harness_execution_id,
        schema_version_id=seeded["schema_version_id"],
        tagger_version_id=seeded["baseline_id"],
        review_bundle_id=f"bundle-{suffix}",
        selection_policy="gold",
        selection_policy_version="1",
        blind_mode=True,
        reason="gold",
        status="resolved",
        priority=100,
        resolved_at=now,
        created_by=1,
    )
    session.add(task)
    await session.flush()
    decision = TagReviewDecision(
        tenant_id="chang_an",
        task_id=task.id,
        action=("reject" if truth_state == "absent" else "accept"),
        corrected_value=gold_value,
        reason_code="gold",
        evidence_refs=(proposed.evidence_refs if truth_state == "present" else []),
        resulting_fact_id=(proposed.id if resulting_fact else None),
        reviewer_user_id=10,
        truth_state=truth_state,
        truth_tier="t2",
        annotator_round=1,
        primary_failure_stage="tag_reasoning",
        reason_codes=["gold"],
        reviewer_confidence=1,
        review_duration_ms=1_000,
        decided_at=now,
    )
    session.add(decision)
    await session.flush()
    session.add(
        TagGoldLabel(
            tenant_id="chang_an",
            gold_set_version_id=seeded["gold_version_id"],
            review_decision_id=decision.id,
            reception_id=reception_id,
            subject_type=subject_type,
            subject_id=subject_id,
            tag_key=tag_key,
            tag_value=gold_value,
            evidence_refs=(proposed.evidence_refs if truth_state == "present" else []),
            truth_state=truth_state,
            truth_tier="t2",
            input_hash=(suffix[0] * 64),
            input_snapshot={
                "reception_id": reception_id,
                "scenario": "automotive",
                "store_id": "S1",
            },
            annotation_quality={},
            cohort=f"bundle-{suffix}",
            completeness_manifest={"complete": True},
            split=split,
        )
    )


@pytest.mark.asyncio
async def test_candidate_uses_frozen_cohort_keeps_absent_and_separates_subject_domains(
    optimizer_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from audio_graphy.models.tag_governance import TagExtractionJob, TagOptimizationRun
    from audio_graphy.services import tag_governance as governance_module
    from audio_graphy.services.tag_governance import (
        TagGovernanceService,
        canonical_checksum,
    )

    seeded = await _seed_cohort_optimizer_context(optimizer_factory)
    resolved_reception_ids = [seeded["target_reception_id"]]
    async with optimizer_factory() as session, session.begin():
        await _add_optimizer_gold_sample(
            session,
            seeded=seeded,
            subject_type="dialogue_unit",
            subject_id=seeded["target_train_unit_id"],
            reception_id=seeded["target_reception_id"],
            tag_key="intent",
            predicted_value="purchase",
            gold_value=None,
            truth_state="absent",
            split="train",
            score=0.9,
            suffix="a1",
            resulting_fact=False,
            baseline_review_tag_keys=("intent",),
        )
        await _add_optimizer_gold_sample(
            session,
            seeded=seeded,
            subject_type="dialogue_unit",
            subject_id=seeded["target_validation_unit_id"],
            reception_id=seeded["target_reception_id"],
            tag_key="intent",
            predicted_value="purchase",
            gold_value="purchase",
            truth_state="present",
            split="validation",
            score=0.8,
            suffix="b2",
            resulting_fact=True,
            baseline_review_tag_keys=(),
        )
        await _add_optimizer_gold_sample(
            session,
            seeded=seeded,
            subject_type="reception",
            subject_id=seeded["target_reception_id"],
            reception_id=seeded["target_reception_id"],
            tag_key="intent",
            predicted_value="purchase",
            gold_value="purchase",
            truth_state="present",
            split="validation",
            # Keep this scope-isolation fixture quality-safe under the
            # optimizer's critical-label recall gate.
            score=0.8,
            suffix="c3",
            resulting_fact=True,
            baseline_review_tag_keys=("objection",),
        )
        await _add_optimizer_gold_sample(
            session,
            seeded=seeded,
            subject_type="dialogue_unit",
            subject_id=seeded["wrong_store_unit_id"],
            reception_id=seeded["wrong_store_reception_id"],
            tag_key="intent",
            predicted_value="purchase",
            gold_value="none",
            truth_state="present",
            split="train",
            score=0.9,
            suffix="d4",
            resulting_fact=True,
            baseline_review_tag_keys=("intent",),
        )
        await _add_optimizer_gold_sample(
            session,
            seeded=seeded,
            subject_type="dialogue_unit",
            subject_id=seeded["target_validation_unit_id"],
            reception_id=seeded["target_reception_id"],
            tag_key="objection",
            predicted_value="price",
            gold_value="none",
            truth_state="present",
            split="train",
            score=0.9,
            suffix="e5",
            resulting_fact=True,
            baseline_review_tag_keys=("objection",),
        )
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=seeded["baseline_id"],
            gold_set_version_id=seeded["gold_version_id"],
            dataset_snapshot_hash="4" * 64,
            trigger="manual",
            status="running",
            phase="search",
            cohort={
                "source": "tag_insights",
                "filters": {
                    "store_ids": ["S1"],
                    "scenarios": ["automotive"],
                    "reception_ids": resolved_reception_ids,
                    "label_keys": ["intent"],
                },
                "resolved_reception_ids": resolved_reception_ids,
                "resolved_reception_checksum": canonical_checksum(
                    {"resolved_reception_ids": resolved_reception_ids}
                ),
            },
            objective={"policy": "balanced"},
            search_budget={"max_trials": 8, "sealed_holdout_queries": 1},
            summary={},
            next_actions=["execute_bounded_search"],
            artifacts=[],
            created_by=9,
        )
        session.add(run)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            origin="system",
            status="running",
            scope={"optimization_run_id": run.id},
            tagger_version_id=seeded["baseline_id"],
            idempotency_key=f"candidate-cohort:{run.id}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="candidate-test-worker",
            lease_token="candidate-test-lease",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            created_by=9,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        run_id = run.id
        job_id = job.id

    original_execute_harness_trials = governance_module.execute_harness_trials
    heartbeat_observed = False

    async def execute_with_heartbeat(**kwargs: Any) -> Any:
        nonlocal heartbeat_observed
        heartbeat_observed = await TagGovernanceService(optimizer_factory).heartbeat_job(
            job_id,
            tenant_id="chang_an",
            worker_id="candidate-test-worker",
            expected_revision=1,
            now=datetime.now(UTC),
            lease_for=timedelta(minutes=5),
        )
        return await original_execute_harness_trials(**kwargs)

    monkeypatch.setattr(
        governance_module,
        "execute_harness_trials",
        execute_with_heartbeat,
    )

    class CompletePolicyReplay:
        materialized_dimensions = frozenset({"output"})

        def __init__(self) -> None:
            self.samples: list[dict[str, Any]] = []

        async def execute_trial(
            self,
            _candidate: dict[str, Any],
            _samples: list[dict[str, Any]],
            **_correlation: Any,
        ) -> dict[str, Any]:
            self.samples = _samples
            return {
                "measurement_source": "scope_isolation_fixture",
                "measurement_complete": True,
                "provider_input_tokens": 0,
                "provider_output_tokens": 0,
                "provider_calls": 0,
                "cost_microunits": 0,
                "feasible": True,
                "quality_delta": 0.0,
                "review_rate_delta": 0.0,
                "p95_latency_delta": 0.0,
                "cost_delta": 0.0,
            }

    policy_replay = CompletePolicyReplay()
    service = TagGovernanceService(
        optimizer_factory,
        optimization_trial_executor=policy_replay,
    )
    candidate, metadata = await service.create_optimization_candidate(
        tenant_id="chang_an",
        gold_set_version_id=seeded["gold_version_id"],
        production_tagger_version_id=seeded["baseline_id"],
        actor_user_id=9,
        optimization_run_id=run_id,
        worker_id="candidate-test-worker",
    )

    assert heartbeat_observed is True
    assert {
        (
            sample["subject_type"],
            sample["subject_id"],
            sample["tag_key"],
        ): sample["baseline_reviewed"]
        for sample in policy_replay.samples
    } == {
        (
            "dialogue_unit",
            seeded["target_train_unit_id"],
            "intent",
        ): True,
        (
            "dialogue_unit",
            seeded["target_validation_unit_id"],
            "intent",
        ): False,
        ("reception", seeded["target_reception_id"], "intent"): False,
    }
    assert metadata["bounded_search"]["eligible_sample_count"] == 3
    assert metadata["train_error_summary"] == {
        "dialogue_unit:intent": [{"error": "'purchase'->None", "count": 1}]
    }
    assert set(metadata["threshold_search"]) == {
        "dialogue_unit:intent",
        "reception:intent",
    }
    assert metadata["threshold_search"]["dialogue_unit:intent"]["sample_count"] == 1
    assert metadata["threshold_search"]["reception:intent"]["sample_count"] == 1
    assert "[AUTO-OPTIMIZATION" not in candidate.prompt_content
    assert "candidate_error_patterns" not in candidate.rule_bundle
    assert metadata["generated_rule_count"] == 0
    assert candidate.harness_spec is not None
    assert candidate.harness_spec["generation"]["prompt_template"] == candidate.prompt_content
    assert candidate.harness_spec["orchestration"]["rule_bundle"] == candidate.rule_bundle
