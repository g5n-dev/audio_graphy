"""Unit tests for the bounded, replayable semantic-tag Harness runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.models.reception import DialogueUnit
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TaggerVersion,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagReviewTask,
    TagSchemaVersion,
)
from audio_graphy.services import tag_extractor as tag_extractor_module
from audio_graphy.services.tag_extractor import TagExtractor
from audio_graphy.services.tag_governance import AssignmentValidationError
from audio_graphy.services.tag_harness_runtime import (
    HarnessSpecError,
    build_scene_profile,
    build_stage_observation,
    estimate_prompt_tokens,
    fuse_assignments,
    materialize_trial_candidate,
    output_token_budget,
    resolve_harness_spec,
)
from tests.services.test_tag_extractor_optimizations import (
    CountingTagLLM,
    _seed_extractor,
)

pytest_plugins = ("tests.services.test_tag_extractor_optimizations",)


def _legacy_tagger(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "engine": "hybrid",
        "prompt_content": "Return strict JSON.",
        "rule_bundle": {"dsl_version": "1", "rules": []},
        "model_version": "weak-v1",
        "thresholds": {"intent": 0.72},
        "harness_spec": {},
        "harness_spec_version": "1.0",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_legacy_tagger_normalizes_to_six_dimension_harness() -> None:
    spec = resolve_harness_spec(_legacy_tagger())

    assert set(spec) == {
        "context",
        "tools",
        "generation",
        "orchestration",
        "memory",
        "output",
    }
    assert spec["generation"]["temperature"] == 0
    assert spec["generation"]["max_input_tokens"] == 12_000
    assert spec["generation"]["budget_policy"] == {
        "max_provider_tokens": None,
        "max_provider_calls": None,
        "max_cost_microunits": None,
        "max_wall_seconds": None,
    }
    assert spec["orchestration"]["route"] == "rule_llm_fusion"
    assert spec["orchestration"]["rule_min_confidence"] == pytest.approx(0.95)
    assert spec["orchestration"]["critic_confidence_margin"] == pytest.approx(0.10)
    assert spec["orchestration"]["critic_max_noncritical_rate"] == pytest.approx(0.20)
    assert spec["output"]["thresholds"] == {"intent": 0.72}
    assert spec["output"]["fallback"] == "review"


def test_explicit_harness_spec_can_enable_registered_strong_critic() -> None:
    spec = resolve_harness_spec(
        _legacy_tagger(
            harness_spec={
                "spec_version": "1.0",
                "context": {
                    "neighbor_units": 1,
                    "example_policy": "hard_negative",
                    "example_top_k": 3,
                },
                "tools": {
                    "primary_model": "weak",
                    "critic_model": "strong",
                },
                "orchestration": {
                    "route": "weak_then_strong_critic",
                    "fusion_policy": "conflict_to_review",
                    "critic_enabled": True,
                },
            }
        )
    )

    assert spec["context"]["neighbor_units"] == 1
    assert spec["context"]["example_policy"] == "hard_negative"
    assert spec["tools"]["critic_model"] == "strong"
    assert spec["orchestration"]["route"] == "weak_then_strong_critic"


def test_optimizer_legacy_harness_spec_is_normalized_and_executable() -> None:
    spec = resolve_harness_spec(
        _legacy_tagger(
            thresholds={"intent": 0.72, "risk": 0.1},
            harness_spec={
                "context": {"neighbor_units": 2},
                "memory": {
                    "example_count": 3,
                    "strategy": "hard_negative",
                },
                "orchestration": {
                    "route": "weak_strong_critic",
                    "fusion": "conflict_to_review",
                },
                "output": {"threshold_offset": 0.05},
            },
        )
    )

    assert spec["context"] == {
        "neighbor_units": 2,
        "example_policy": "hard_negative",
        "example_top_k": 3,
    }
    assert spec["orchestration"]["route"] == "weak_then_strong_critic"
    assert spec["orchestration"]["fusion_policy"] == "conflict_to_review"
    assert spec["orchestration"]["critic_enabled"] is True
    assert spec["tools"]["critic_model"] == "strong"
    assert spec["output"]["thresholds"] == {
        "intent": pytest.approx(0.77),
        "risk": pytest.approx(0.15),
    }


def test_precanonical_pydantic_harness_spec_is_normalized() -> None:
    spec = resolve_harness_spec(
        _legacy_tagger(
            harness_spec={
                "spec_version": "1.0",
                "context": {
                    "neighbor_window": 1,
                    "example_count": 6,
                    "example_strategy": "mixed",
                },
                "tools": {
                    "rule_engine_enabled": True,
                    "weak_model": "weak-v1",
                    "strong_model": "strong-v1",
                    "critic_enabled": True,
                },
                "generation": {
                    "max_output_tokens": 1024,
                    "temperature": 0,
                },
                "orchestration": {
                    "mode": "weak_strong_critic",
                    "fusion_policy": "score_priority",
                },
                "memory": {
                    "enabled": True,
                    "retrieval_strategy": "approved_cases",
                    "top_k": 6,
                },
                "output": {
                    "validate_schema": True,
                    "evidence_required": True,
                    "default_confidence_threshold": 0.76,
                    "abstention_threshold": 0.42,
                },
            },
        )
    )

    assert spec["context"]["neighbor_units"] == 1
    assert spec["context"]["example_policy"] == "mixed"
    assert spec["context"]["example_top_k"] == 6
    assert spec["generation"]["max_tokens"] == 1024
    assert spec["orchestration"]["route"] == "weak_then_strong_critic"
    assert spec["tools"]["critic_model"] == "strong"
    assert spec["memory"] == {"policy": "approved_cases", "top_k": 6}
    assert spec["output"]["schema_validation"] is True
    assert spec["output"]["evidence_validation"] is True
    assert spec["output"]["review_threshold"] == pytest.approx(0.76)
    assert spec["output"]["abstain_threshold"] == pytest.approx(0.42)


def test_harness_v2_accepts_bounded_token_and_budget_policy() -> None:
    spec = resolve_harness_spec(
        _legacy_tagger(
            harness_spec_version="2.0",
            harness_spec={
                "spec_version": "2.0",
                "generation": {
                    "max_input_tokens": 12_000,
                    "max_tokens": 256,
                    "budget_policy": {
                        "max_provider_tokens": 50_000,
                        "max_provider_calls": 50,
                        "max_cost_microunits": 250_000,
                        "max_wall_seconds": 900,
                    },
                },
                "orchestration": {
                    "rule_min_confidence": 0.95,
                    "critic_confidence_margin": 0.10,
                    "critic_max_noncritical_rate": 0.20,
                },
            },
        )
    )

    assert spec["generation"]["max_tokens"] == 256
    assert spec["generation"]["budget_policy"]["max_provider_calls"] == 50
    assert spec["orchestration"]["critic_max_noncritical_rate"] == pytest.approx(0.20)


def test_unknown_harness_spec_version_is_rejected() -> None:
    with pytest.raises(HarnessSpecError):
        resolve_harness_spec(_legacy_tagger(harness_spec={"spec_version": "3.0"}))


@pytest.mark.parametrize(
    ("tag_count", "configured_cap", "expected"),
    [
        (0, 2048, 256),
        (1, 2048, 256),
        (2, 2048, 512),
        (8, 2048, 1024),
        (10, 2048, 2048),
        (10, 1024, 1024),
    ],
)
def test_output_token_budget_uses_bounded_buckets(
    tag_count: int,
    configured_cap: int,
    expected: int,
) -> None:
    assert output_token_budget(tag_count, configured_cap=configured_cap) == expected


def test_trial_prompt_and_rules_are_materialized_before_evaluation() -> None:
    baseline = resolve_harness_spec(_legacy_tagger())
    prompt_delta = "Only emit labels supported by explicit transcript evidence."
    rule_bundle = {
        "dsl_version": "1",
        "rules": [
            {
                "tag_key": "intent",
                "value": "purchase",
                "contains_any": ["购买"],
                "confidence": 0.95,
            }
        ],
    }

    candidate = materialize_trial_candidate(
        baseline,
        prompt_delta=prompt_delta,
        rule_bundle=rule_bundle,
    )

    assert candidate["generation"]["prompt_template"].endswith(prompt_delta)
    assert candidate["orchestration"]["rule_bundle"] == rule_bundle
    assert estimate_prompt_tokens(prompt_delta) <= 512
    assert baseline["generation"]["prompt_template"] == "Return strict JSON."
    assert baseline["orchestration"]["rule_bundle"] != rule_bundle


def test_trial_prompt_delta_rejects_more_than_512_proxy_tokens() -> None:
    with pytest.raises(HarnessSpecError, match="512"):
        materialize_trial_candidate(
            resolve_harness_spec(_legacy_tagger()),
            prompt_delta="超" * 513,
        )


def test_replace_mode_swaps_the_whole_policy_section() -> None:
    baseline = resolve_harness_spec(_legacy_tagger())
    compiled = "判定规则：仅在转写中出现明确金额时输出价格标签。\n\n示例：……"

    candidate = materialize_trial_candidate(
        baseline,
        prompt_mode="replace",
        prompt_template=compiled,
    )

    assert candidate["generation"]["prompt_template"] == compiled
    assert baseline["generation"]["prompt_template"] == "Return strict JSON."


def test_replace_mode_accepts_prompts_beyond_the_append_delta_budget() -> None:
    """A compiled prompt with inlined demos cannot fit the 512-token delta budget."""

    compiled = "规则。" * 400
    assert estimate_prompt_tokens(compiled) > 512

    candidate = materialize_trial_candidate(
        resolve_harness_spec(_legacy_tagger()),
        prompt_mode="replace",
        prompt_template=compiled,
    )

    assert candidate["generation"]["prompt_template"] == compiled


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"prompt_mode": "replace"}, "requires a prompt template"),
        ({"prompt_mode": "replace", "prompt_template": "   "}, "non-empty"),
        (
            {"prompt_mode": "replace", "prompt_template": "x", "prompt_delta": "y"},
            "cannot be combined",
        ),
        ({"prompt_template": "x"}, "only accepted in replace mode"),
        ({"prompt_mode": "rewrite"}, "append or replace"),
        (
            {"prompt_mode": "replace", "prompt_template": "超" * 4_000},
            "proxy budget",
        ),
        (
            {
                "prompt_mode": "replace",
                "prompt_template": "ok",
                "max_prompt_template_tokens": 12_001,
            },
            "must be between 1 and 12000",
        ),
    ],
)
def test_replace_mode_rejects_unsafe_combinations(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(HarnessSpecError, match=match):
        materialize_trial_candidate(resolve_harness_spec(_legacy_tagger()), **kwargs)


def test_malformed_registered_tools_raises_harness_error() -> None:
    with pytest.raises(HarnessSpecError, match="registered_tools"):
        resolve_harness_spec(
            _legacy_tagger(
                harness_spec={
                    "tools": {
                        "registered_tools": None,
                    }
                }
            )
        )


@pytest.mark.parametrize(
    ("section", "payload"),
    [
        ("tools", {"primary_model": "unregistered"}),
        ("orchestration", {"route": "arbitrary_dag"}),
        ("context", {"neighbor_units": 99}),
        ("generation", {"temperature": 0.7}),
    ],
)
def test_harness_rejects_unbounded_action_space(
    section: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(HarnessSpecError):
        resolve_harness_spec(_legacy_tagger(harness_spec={section: payload}))


def test_scene_profile_uses_only_stable_available_signals() -> None:
    segments = [
        SimpleNamespace(start_sec=0.0, end_sec=2.5, speaker="customer", vad_conf=0.8),
        SimpleNamespace(start_sec=2.5, end_sec=5.0, speaker="agent", vad_conf=1.0),
        SimpleNamespace(start_sec=5.0, end_sec=7.0, speaker="customer", vad_conf=None),
    ]

    profile = build_scene_profile(
        scenario="automotive",
        subject_type="dialogue_unit",
        transcript="客户想试驾\n销售介绍车型",
        segments=segments,
    )

    assert profile == {
        "scenario": "automotive",
        "store_id": None,
        "subject_type": "dialogue_unit",
        "duration_sec": 7.0,
        "segment_count": 3,
        "speaker_count": 2,
        "average_vad_confidence": 0.9,
        "transcript_char_count": 11,
        "snr": None,
        "overlap_ratio": None,
        "asr_confidence": None,
        "diarization_confidence": None,
    }


def test_stage_observation_has_recovery_contract() -> None:
    observation = build_stage_observation(
        status="warning",
        summary="strong critic unavailable; routed to human review",
        next_actions=["create_review_task"],
        artifacts=["harness_execution:42"],
        details={"route": "review"},
    )

    assert observation == {
        "status": "warning",
        "summary": "strong critic unavailable; routed to human review",
        "next_actions": ["create_review_task"],
        "artifacts": ["harness_execution:42"],
        "details": {"route": "review"},
    }


def test_conflict_to_review_never_silently_overwrites_disagreement() -> None:
    selected, conflicts = fuse_assignments(
        {
            "rule": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.92,
                    "source": "rule",
                }
            },
            "weak": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "browse",
                    "confidence": 0.95,
                    "source": "llm",
                }
            },
        },
        policy="conflict_to_review",
    )

    assert selected == {}
    assert conflicts == ("intent",)


def test_score_priority_is_deterministic_and_keeps_highest_confidence() -> None:
    selected, conflicts = fuse_assignments(
        {
            "weak": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "browse",
                    "confidence": 0.8,
                    "source": "llm",
                }
            },
            "critic": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.91,
                    "source": "critic",
                }
            },
        },
        policy="score_priority",
    )

    assert selected["intent"]["tag_value"] == "purchase"
    assert conflicts == ("intent",)


def test_rule_priority_preserves_legacy_behavior_explicitly() -> None:
    selected, conflicts = fuse_assignments(
        {
            "weak": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "browse",
                    "confidence": 0.99,
                    "source": "llm",
                }
            },
            "rule": {
                "intent": {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.8,
                    "source": "rule",
                }
            },
        },
        policy="rule_priority",
    )

    assert selected["intent"]["tag_value"] == "purchase"
    assert conflicts == ("intent",)


async def _set_harness_spec(
    factory: async_sessionmaker[AsyncSession],
    tagger_version_id: int,
    spec: dict[str, object],
) -> None:
    async with factory() as session, session.begin():
        tagger = await session.get(TaggerVersion, tagger_version_id)
        assert tagger is not None
        tagger.harness_spec = spec


class FixedValueTagLLM:
    model = "fixed-tag-model"
    provider = "test"

    def __init__(
        self,
        value: str,
        *,
        model: str = "fixed-tag-model",
        confidence: float = 0.95,
    ) -> None:
        self.value = value
        self.model = model
        self.confidence = confidence
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        del messages, temperature, max_tokens, cache_key
        assert response_schema is not None
        self.calls += 1
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": self.value,
                            "confidence": self.confidence,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model=self.model,
            prompt_hash=f"fixed-{self.value}-{self.calls}",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


@pytest.mark.asyncio
async def test_rule_only_harness_route_skips_llm(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    await _set_harness_spec(
        extractor_factory,
        seeded.tagger_version_id,
        {"orchestration": {"route": "rule_only"}},
    )
    llm = CountingTagLLM()

    result = await TagExtractor(
        extractor_factory,
        weak_llm=llm,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert llm.calls == 0
    assert result.route == "rule_only"
    assert result.assignments[0]["tag_value"] == "purchase"


@pytest.mark.asyncio
async def test_conflict_to_review_does_not_publish_a_silent_winner(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    await _set_harness_spec(
        extractor_factory,
        seeded.tagger_version_id,
        {
            "orchestration": {
                "route": "rule_llm_fusion",
                "fusion_policy": "conflict_to_review",
            }
        },
    )

    result = await TagExtractor(
        extractor_factory,
        weak_llm=FixedValueTagLLM("browse"),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert result.assignments == ()
    assert result.conflict_tag_keys == ("intent",)
    assert any(item["reason"] == "conflict" for item in result.review_items)


@pytest.mark.asyncio
async def test_registered_strong_model_can_criticize_weak_output(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    await _set_harness_spec(
        extractor_factory,
        seeded.tagger_version_id,
        {
            "tools": {
                "primary_model": "weak",
                "critic_model": "strong",
            },
            "orchestration": {
                "route": "weak_then_strong_critic",
                "fusion_policy": "score_priority",
                "critic_enabled": True,
            },
        },
    )
    weak = FixedValueTagLLM(
        "browse",
        model="weak-fixed-tag-model",
        confidence=0.72,
    )
    strong = FixedValueTagLLM("purchase", model="strong-fixed-tag-model")

    result = await TagExtractor(
        extractor_factory,
        weak_llm=weak,
        strong_llm=strong,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert weak.calls == 1
    assert strong.calls == 1
    assert result.assignments[0]["tag_value"] == "purchase"
    assert result.route == "weak_then_strong_critic"


@pytest.mark.asyncio
async def test_predict_reception_keeps_reception_label_domain_separate(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    async with extractor_factory() as session, session.begin():
        unit = await session.get(DialogueUnit, seeded.dialogue_unit_id)
        assert unit is not None
        tagger = await session.get(TaggerVersion, seeded.tagger_version_id)
        assert tagger is not None
        schema = await session.get(TagSchemaVersion, tagger.schema_version_id)
        assert schema is not None
        schema.definitions = [
            {
                **definition,
                "subject_types": ["dialogue_unit", "reception"],
            }
            for definition in schema.definitions
        ]
        reception_id = unit.reception_id

    result = await TagExtractor(extractor_factory).predict_reception(
        tenant_id="chang_an",
        reception_id=reception_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert result.assignments[0]["tag_key"] == "intent"
    assert result.assignments[0]["tag_value"] == "purchase"
    assert result.input_snapshot["subject_type"] == "reception"
    assert result.input_snapshot["subject_id"] == reception_id
    assert result.input_snapshot["reception_id"] == reception_id
    assert result.scene_profile["subject_type"] == "reception"

    async with extractor_factory() as session, session.begin():
        segment = await session.get(
            Segment,
            int(result.input_snapshot["segments"][0]["segment_id"]),
        )
        assert segment is not None
        segment.transcript = "客户只想浏览"
        segment.text_scrubbed = "客户只想浏览"

    replay = await TagExtractor(extractor_factory).predict_frozen_input(
        tenant_id="chang_an",
        subject_type="reception",
        subject_id=reception_id,
        input_snapshot=result.input_snapshot,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert replay.assignments[0]["tag_value"] == "purchase"
    assert replay.scene_profile["subject_type"] == "reception"
    assert replay.input_hash == result.input_hash


@pytest.mark.asyncio
async def test_predict_frozen_input_never_falls_back_to_live_dialogue_data(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    extractor = TagExtractor(extractor_factory)
    live = await extractor.predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )
    assert live.assignments[0]["tag_value"] == "purchase"

    async with extractor_factory() as session, session.begin():
        unit = await session.get(DialogueUnit, seeded.dialogue_unit_id)
        assert unit is not None
        segment_id = int(live.input_snapshot["segments"][0]["segment_id"])
        segment = await session.get(Segment, segment_id)
        assert segment is not None
        segment.transcript = "客户只想浏览"
        segment.text_scrubbed = "客户只想浏览"
        unit.version += 1

    replay = await extractor.predict_frozen_input(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=seeded.dialogue_unit_id,
        input_snapshot=live.input_snapshot,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert replay.assignments[0]["tag_value"] == "purchase"
    assert replay.input_snapshot["transcript"] == "客户决定购买"
    assert replay.scene_profile["subject_type"] == "dialogue_unit"
    assert replay.input_hash == live.input_hash


@pytest.mark.asyncio
async def test_predict_frozen_input_rejects_cross_subject_domain_snapshot(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    live = await TagExtractor(extractor_factory).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    with pytest.raises(AssignmentValidationError, match="subject"):
        await TagExtractor(extractor_factory).predict_frozen_input(
            tenant_id="chang_an",
            subject_type="reception",
            subject_id=int(live.input_snapshot["reception_id"]),
            input_snapshot=live.input_snapshot,
            tagger_version_id=seeded.tagger_version_id,
        )


@pytest.mark.asyncio
async def test_extraction_persists_six_stage_replay_trace(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    monkeypatch.setattr(
        tag_extractor_module,
        "compute_input_hash",
        lambda *_args, **_kwargs: "0" * 64,
    )

    result = await TagExtractor(
        extractor_factory,
        weak_llm=FixedValueTagLLM("purchase"),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    async with extractor_factory() as session:
        execution = (
            await session.execute(
                select(TagHarnessExecution).where(
                    TagHarnessExecution.extraction_run_id == result.run_id
                )
            )
        ).scalar_one()
        traces = list(
            (
                await session.execute(
                    select(TagHarnessStageTrace)
                    .where(TagHarnessStageTrace.harness_execution_id == execution.id)
                    .order_by(TagHarnessStageTrace.sequence_no)
                )
            )
            .scalars()
            .all()
        )
        review_tasks = list((await session.execute(select(TagReviewTask))).scalars())

    assert execution.status == "completed"
    assert execution.route == "rule_llm_fusion"
    assert execution.scene_profile["segment_count"] == 1
    assert execution.resolved_harness_spec["output"]["fallback"] == "review"
    assert execution.token_count == 15
    assert execution.output_snapshot["review_item_count"] == 1
    assert execution.output_snapshot["review_items"] == [{"tag_key": "intent"}]
    assert set(execution.output_snapshot["review_items"][0]) == {"tag_key"}
    assert [trace.stage for trace in traces] == [
        "context",
        "tools",
        "generation",
        "orchestration",
        "memory",
        "output",
    ]
    assert all(
        set(trace.observation)
        >= {
            "status",
            "summary",
            "next_actions",
            "artifacts",
        }
        for trace in traces
    )
    # A direct/system extraction is replayable, but is not an unbiased serving
    # sample and therefore must never mint a trusted representative audit.
    assert review_tasks == []

    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TagGoldSet,
        TagGoldSetVersion,
    )

    now = datetime.now(UTC)
    async with extractor_factory() as session, session.begin():
        tagger = await session.get(TaggerVersion, seeded.tagger_version_id)
        assert tagger is not None
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="serving-audit-contract",
            name="Serving audit contract",
            schema_version_id=tagger.schema_version_id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        gold_version = TagGoldSetVersion(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="1",
            status="frozen",
            checksum="1" * 64,
            dataset_snapshot_hash="2" * 64,
            completeness_manifest={},
            item_count=0,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(gold_version)
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=tagger.id,
            baseline_tagger_version_id=tagger.id,
            gold_set_version_id=gold_version.id,
            dataset_snapshot_hash="2" * 64,
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
        deployment = TagDeployment(
            tenant_id="chang_an",
            tagger_version_id=tagger.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=tagger.id,
            status="shadow",
            traffic_percent=0,
            revision=1,
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        deployment_id = deployment.id

    serving = await TagExtractor(
        extractor_factory,
        weak_llm=FixedValueTagLLM("purchase"),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.second_job_id,
        deployment_id=deployment_id,
        actor_user_id=1,
        publish_current=False,
        run_origin="serving",
        served_current=False,
    )

    async with extractor_factory() as session:
        serving_tasks = list((await session.execute(select(TagReviewTask))).scalars())

    representative_tasks = [task for task in serving_tasks if task.reason == "random"]
    assert representative_tasks
    assert {task.selection_policy for task in representative_tasks} == {
        "representative_audit"
    }
    assert all(
        task.sampling_probability == pytest.approx(0.05) for task in representative_tasks
    )
    assert {task.source_extraction_run_id for task in representative_tasks} == {serving.run_id}
    assert {task.source_deployment_id for task in representative_tasks} == {deployment_id}
    assert {task.sampled_deployment_stage for task in representative_tasks} == {"shadow"}
    assert {task.sampled_deployment_revision for task in representative_tasks} == {1}
    assert all(task.sampling_manifest_checksum is not None for task in representative_tasks)
    assert all(task.blind_mode for task in representative_tasks)


@pytest.mark.asyncio
async def test_cached_extraction_persists_zero_cost_reuse_trace(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    llm = FixedValueTagLLM("purchase")
    extractor = TagExtractor(extractor_factory, weak_llm=llm)
    await extractor.extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    cached = await extractor.extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.second_job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    async with extractor_factory() as session:
        execution = (
            await session.execute(
                select(TagHarnessExecution).where(
                    TagHarnessExecution.extraction_run_id == cached.run_id
                )
            )
        ).scalar_one()
        traces = list(
            (
                await session.execute(
                    select(TagHarnessStageTrace).where(
                        TagHarnessStageTrace.harness_execution_id == execution.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert cached.cached is True
    assert llm.calls == 1
    assert execution.route == "cache_reuse"
    assert execution.token_count == 0
    assert len(traces) == 6
