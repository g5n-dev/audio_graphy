"""Release-gate tests for the frozen gold/automotive dialogue corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.core.dialogue_segmentation import DialogueSegmenter
from audio_graphy.eval.dialogue_segmentation import (
    evaluate_dialogue_release,
    load_dialogue_gold,
)

_GOLD_PATH = (
    Path(__file__).parents[1] / "fixtures" / "dialogue_segmentation_gold.json"
)


def test_dialogue_hybrid_v2_clears_global_and_per_scenario_release_gates() -> None:
    cases, v1_baselines = load_dialogue_gold(_GOLD_PATH)

    decision = evaluate_dialogue_release(
        cases,
        v1_baseline_by_scenario=v1_baselines,
    )

    assert decision.publish_v2_default is True, decision.failures
    assert decision.metrics.boundary.f1 >= 0.85
    assert decision.metrics.stage_macro_f1 >= 0.80
    assert decision.metrics.case_count == 4
    assert decision.metrics.segment_count == 19
    for scenario, baseline in v1_baselines.items():
        assert (
            decision.metrics.boundary_f1_by_scenario[scenario]
            >= baseline["boundary_f1"]
        )
        assert (
            decision.metrics.stage_macro_f1_by_scenario[scenario]
            >= baseline["stage_macro_f1"]
        )


def test_regressed_candidate_is_not_promoted_even_if_code_is_publishable() -> None:
    cases, v1_baselines = load_dialogue_gold(_GOLD_PATH)

    decision = evaluate_dialogue_release(
        cases,
        v1_baseline_by_scenario=v1_baselines,
        segmenter=DialogueSegmenter(boundary_threshold=1.0),
    )

    assert decision.publish_v2_default is False
    assert any("boundary F1" in failure for failure in decision.failures)


def test_gold_loader_rejects_missing_scenario_baseline(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        """
        {
          "v1_baseline_by_scenario": {},
          "cases": [{
            "case_id": "missing-baseline",
            "scenario": "automotive",
            "segments": [{
              "segment_id": "s1",
              "start_sec": 0,
              "end_sec": 1,
              "transcript": "您好",
              "speaker": "agent"
            }],
            "boundary_after_segment_ids": [],
            "stage_by_segment_id": {"s1": "greeting"}
          }]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing v1 baseline"):
        load_dialogue_gold(invalid)
