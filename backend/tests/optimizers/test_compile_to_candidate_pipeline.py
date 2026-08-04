"""The MVP loop, end to end, with no optional extras installed.

Reviewed badcases -> compiled prompt -> injected candidate -> real replay -> gate.

Each step is covered on its own elsewhere; this file exists to catch the seams, which
is where a prompt candidate would silently be dropped: truncated out of the envelope,
rejected by an input budget it never should have reached, or vetoed by an efficiency
rule that was never meant to judge a prompt that trades tokens for accuracy.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from audio_graphy.optimizers.proposers import (
    BuiltinProposer,
    ProposalRequest,
    cluster_badcases,
)
from audio_graphy.services.tag_extractor import (
    QUALITY_UPLIFT_V1,
    TagExtractorHarnessTrialExecutor,
    prompt_input_budget_report,
)
from audio_graphy.services.tag_governance import (
    InjectedCandidate,
    _bounded_candidate_configs,
)
from audio_graphy.services.tag_harness_runtime import materialize_trial_candidate

_BASELINE_PROMPT = "基线规则：按 schema 判定标签。"
_DEFINITIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "key": "intent",
        "value_type": "enum",
        "allowed_values": ["purchase", "browse"],
    }
}


def _reviewed_badcases() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "tag_key": "intent",
            "failure_stage": "tag_reasoning",
            "cluster_key": "tag_reasoning:intent:missed_label",
            "occurrence_count": 8,
            "root_cause": {
                "reason_code": "missed_label",
                "truth_state": "present",
                "upstream_routed": False,
            },
        },
        {
            # Must be dropped: a garbled transcript is not something a prompt can fix.
            "id": 2,
            "tag_key": "intent",
            "failure_stage": "asr",
            "cluster_key": "asr:intent:garbled",
            "occurrence_count": 30,
            "root_cause": {"reason_code": "garbled", "upstream_routed": True},
        },
    ]


def _baseline_spec() -> dict[str, Any]:
    return {
        "generation": {
            "prompt_template": _BASELINE_PROMPT,
            "max_input_tokens": 12_000,
            "max_tokens": 512,
        },
        "orchestration": {"route": "weak_llm"},
        "output": {"review_threshold": 0.7, "thresholds": {"intent": 0.7}},
    }


@pytest.fixture
def compiled_prompt() -> str:
    artifact = BuiltinProposer().propose(
        ProposalRequest(
            baseline_prompt=_BASELINE_PROMPT,
            clusters=cluster_badcases(_reviewed_badcases()),
            definitions=_DEFINITIONS,
        )
    )
    assert artifact.patches, "the reviewed cluster should have produced advice"
    return artifact.render()


def test_compiled_prompt_excludes_upstream_noise_and_keeps_the_baseline(
    compiled_prompt: str,
) -> None:
    assert compiled_prompt.startswith(_BASELINE_PROMPT)
    assert "intent" in compiled_prompt
    assert "garbled" not in compiled_prompt


def test_compiled_prompt_fits_the_input_budget_it_will_serve_under(
    compiled_prompt: str,
) -> None:
    baseline = _baseline_spec()
    candidate = materialize_trial_candidate(
        baseline,
        prompt_mode="replace",
        prompt_template=compiled_prompt,
    )

    report = prompt_input_budget_report(
        candidate,
        baseline=baseline,
        definitions=_DEFINITIONS,
    )

    assert report.fits is True
    assert report.headroom_delta < 0, "the compiled prompt does cost headroom"
    assert report.headroom_shrink_ratio < 0.5, "but nowhere near enough to force batching"


def test_compiled_prompt_reaches_the_trial_envelope_without_being_truncated(
    compiled_prompt: str,
) -> None:
    baseline = _baseline_spec()
    injected = InjectedCandidate(
        mutation="generation.prompt_template=builtin#compiled",
        config=materialize_trial_candidate(
            baseline,
            prompt_mode="replace",
            prompt_template=compiled_prompt,
        ),
        provenance={"compiler": "builtin"},
    )

    envelope = _bounded_candidate_configs(
        baseline,
        materialized_dimensions=frozenset({"generation", "orchestration", "output"}),
        extra_candidates=[injected],
    )

    assert len(envelope) > 2, "the mechanical sweep still contributes candidates"
    assert envelope[1][0] == "generation.prompt_template=builtin#compiled"
    assert envelope[1][1]["generation"]["prompt_template"] == compiled_prompt


@pytest.mark.asyncio
async def test_the_compiled_candidate_is_judged_on_quality_not_vetoed_on_length(
    compiled_prompt: str,
) -> None:
    """The whole point of the uplift envelope: a longer prompt gets to be measured."""

    class Predictor:
        def __init__(self) -> None:
            self.seen_prompts: list[str] = []

        async def predict_materialized_frozen_input(self, **kwargs: Any) -> Any:
            self.seen_prompts.append(kwargs["harness_spec"]["generation"]["prompt_template"])
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
                latency_ms=28,
                # A longer prompt genuinely costs more than the baseline spent.
                provider_input_tokens=130,
                provider_output_tokens=20,
                reused_input_tokens=0,
                reused_output_tokens=0,
                provider_calls=1,
                cache_hits=0,
                strong_escalations=0,
                cost_microunits=22,
            )

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
    candidate = materialize_trial_candidate(
        _baseline_spec(),
        prompt_mode="replace",
        prompt_template=compiled_prompt,
    )

    predictor = Predictor()
    default = await TagExtractorHarnessTrialExecutor(predictor).execute_trial(  # type: ignore[arg-type]
        candidate,
        [dict(sample)],
    )
    uplift = await TagExtractorHarnessTrialExecutor(
        Predictor(),  # type: ignore[arg-type]
        efficiency_envelope=QUALITY_UPLIFT_V1,
    ).execute_trial(candidate, [dict(sample)])

    assert predictor.seen_prompts == [compiled_prompt], "the replay used the compiled text"
    assert default["efficiency_gate_passed"] is False
    assert uplift["efficiency_gate_passed"] is True
    assert uplift["quality_delta"] > 0
    assert [flip["direction"] for flip in uplift["flips"]] == ["fixed"]
