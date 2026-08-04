"""Textual-gradient repair: the parts that decide, tested without the library.

Three things carry consequences here. A diagnosis that invents a cause reads to a
reviewer exactly like a finding. An edit that survives validation but contradicts the
baseline contract cancels it out, and the metric shows noise rather than the
regression it looks like. And an effect record drawn from four samples, reported
without a caveat, is the most persuasive wrong number in the whole feature.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.optimizers.gradients import (
    EVALUATION_SYSTEM,
    LOW_CONFIDENCE_SUPPORT,
    TEXTGRAD_COMPILER_VERSION,
    TGD_CONSTRAINTS,
    GradientOutcome,
    TextGradProposer,
    build_evaluation_prompt,
    build_evaluation_record,
    gradient_rows,
    tag_key_deltas,
)
from audio_graphy.optimizers.proposers import ProposalRequest, cluster_badcases

_DEFINITIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "key": "intent",
        "description": "客户的购车意向阶段。",
        "value_type": "enum",
        "allowed_values": ["purchase", "browse"],
    },
}

_EDIT = "标签 intent 只有在客户出现明确购买动作或表态时才判定为 purchase。"


class StubStep:
    """Records what the proposer asked for and returns a canned round."""

    def __init__(self, outcome: GradientOutcome | Exception | None = None) -> None:
        self.outcome = outcome or GradientOutcome(
            gradient_text="现行规则只认明确表述，未覆盖间接表达。",
            proposed_edit=_EDIT,
            rounds=2,
        )
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        current_rule: str,
        evaluation_prompt: str,
        role_description: str,
        iterations: int,
    ) -> GradientOutcome:
        self.calls.append(
            {
                "current_rule": current_rule,
                "evaluation_prompt": evaluation_prompt,
                "role_description": role_description,
                "iterations": iterations,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _badcase(
    badcase_id: int,
    *,
    occurrences: int = 6,
    tag_key: str = "intent",
    cluster_key: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": badcase_id,
        "tag_key": tag_key,
        "failure_stage": "tag_reasoning",
        "occurrence_count": occurrences,
        "root_cause": {"reason_code": "missed_label", "truth_state": "present"},
    }
    if cluster_key:
        row["cluster_key"] = cluster_key
    return row


def _propose(step: StubStep, rows: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
    proposer = TextGradProposer(step=step, **kwargs)
    artifact = proposer.propose(
        ProposalRequest(
            baseline_prompt="基线规则：按 schema 判定标签。",
            clusters=cluster_badcases(rows if rows is not None else [_badcase(1)]),
            definitions=_DEFINITIONS,
        )
    )
    return artifact, proposer


# ------------------------------------------------------------ evaluation prompt


def test_the_critique_instruction_travels_with_the_facts() -> None:
    """A constant that only filled in as a fallback would constrain nothing."""

    cluster = cluster_badcases([_badcase(1)])[0]

    prompt = build_evaluation_prompt(cluster, _DEFINITIONS["intent"], baseline_prompt="基线规则")

    assert EVALUATION_SYSTEM in prompt
    assert "只诊断" in prompt


def test_the_prompt_states_only_facts_a_reviewer_already_signed_off_on() -> None:
    cluster = cluster_badcases([_badcase(1)])[0]

    prompt = build_evaluation_prompt(cluster, _DEFINITIONS["intent"], baseline_prompt="基线规则")

    assert "intent" in prompt
    assert "missed_label" in prompt
    assert "purchase、browse" in prompt
    assert "基线规则" in prompt


def test_a_thin_cluster_warns_the_critic_before_it_answers() -> None:
    cluster = cluster_badcases([_badcase(1, occurrences=4)])[0]

    assert "样本量偏小" in build_evaluation_prompt(cluster, None, baseline_prompt="基线")


def test_a_well_supported_cluster_carries_no_such_caveat() -> None:
    cluster = cluster_badcases([_badcase(1, occurrences=40)])[0]

    assert "样本量偏小" not in build_evaluation_prompt(cluster, None, baseline_prompt="基线")


def test_an_absent_baseline_is_stated_rather_than_left_blank() -> None:
    cluster = cluster_badcases([_badcase(1)])[0]

    assert "（无）" in build_evaluation_prompt(cluster, None, baseline_prompt="   ")


def test_the_descent_constraints_forbid_guessing() -> None:
    # The baseline system prompt already says to omit unsupported labels; an edit
    # that invited guessing would leave the two halves of the prompt disagreeing.
    assert any("缺乏直接文本依据" in constraint for constraint in TGD_CONSTRAINTS)


# ------------------------------------------------------------------- proposing


def test_the_edited_rule_becomes_the_patch_body() -> None:
    artifact, _ = _propose(StubStep())

    (patch,) = artifact.patches
    assert patch.body == _EDIT
    assert patch.origin == "textgrad_tgd"
    assert artifact.compiler == "textgrad_tgd"
    assert artifact.compiler_version == TEXTGRAD_COMPILER_VERSION


def test_the_step_is_handed_the_template_rule_as_its_starting_point() -> None:
    """Starting from nothing would discard the one statement known to be true."""

    step = StubStep()

    _propose(step)

    assert "intent" in step.calls[0]["current_rule"]
    assert step.calls[0]["role_description"] == "标签 intent 的判定规则"


def test_the_iteration_count_reaches_the_step() -> None:
    step = StubStep()

    _propose(step, iterations=3)

    assert step.calls[0]["iterations"] == 3


def test_an_edit_that_fails_validation_degrades_to_the_template() -> None:
    step = StubStep(GradientOutcome(gradient_text="诊断", proposed_edit="随便写点什么。"))

    artifact, proposer = _propose(step)

    (patch,) = artifact.patches
    assert "取值判错" in patch.body, "the deterministic template must still be there"
    assert "未通过校验" in patch.rationale
    # The diagnosis is what a reviewer reads to judge whether the fallback was right.
    assert proposer.gradients[patch.patch_id]["gradient_text"] == "诊断"


def test_a_failed_step_degrades_to_the_template_and_records_zero_rounds() -> None:
    step = StubStep(RuntimeError("provider timeout"))

    artifact, proposer = _propose(step)

    (patch,) = artifact.patches
    assert "取值判错" in patch.body
    assert "梯度步骤失败" in patch.rationale
    assert proposer.gradients[patch.patch_id]["gradient_rounds"] == 0
    assert proposer.gradients[patch.patch_id]["gradient_text"] == ""


def test_no_eligible_cluster_means_no_step_and_no_patches() -> None:
    step = StubStep()

    artifact, proposer = _propose(step, [_badcase(1, occurrences=1)])

    assert artifact.patches == ()
    assert step.calls == []
    assert proposer.gradients == {}


def test_two_clusters_reaching_the_same_rule_are_recorded_as_one_patch() -> None:
    """Their evidence merges rather than one silently overwriting the other."""

    step = StubStep()

    artifact, proposer = _propose(
        step,
        [
            _badcase(1, cluster_key="c1", occurrences=6),
            _badcase(2, cluster_key="c2", occurrences=6),
        ],
    )

    (patch,) = artifact.patches
    record = proposer.gradients[patch.patch_id]
    assert record["cluster_support"] == 12
    assert record["source_badcase_count"] == 2
    assert len(step.calls) == 2, "each cluster still gets its own diagnosis"


# ------------------------------------------------------------- effect recording


def test_an_unreplayed_patch_says_so_instead_of_reporting_a_zero() -> None:
    """The panel renders any numeric field; a fabricated zero reads as a regression."""

    cluster = cluster_badcases([_badcase(1, occurrences=40)])[0]

    record = build_evaluation_record(cluster, rounds=2)

    assert record["replayed"] is False
    assert "tag_key_deltas" not in record
    assert "macro_f1_delta" not in record


def test_a_thin_cluster_is_flagged_low_confidence() -> None:
    cluster = cluster_badcases([_badcase(1, occurrences=LOW_CONFIDENCE_SUPPORT - 1)])[0]

    assert build_evaluation_record(cluster, rounds=1)["low_confidence"] is True


def test_a_well_supported_cluster_is_not_flagged() -> None:
    cluster = cluster_badcases([_badcase(1, occurrences=LOW_CONFIDENCE_SUPPORT)])[0]

    assert build_evaluation_record(cluster, rounds=1)["low_confidence"] is False


def test_a_replay_contributes_the_side_effects_it_measured() -> None:
    cluster = cluster_badcases([_badcase(1, occurrences=40)])[0]

    record = build_evaluation_record(
        cluster,
        rounds=1,
        replay={
            "baseline_label_f1": {"intent": 0.80, "price": 0.72},
            "candidate_label_f1": {"intent": 0.86, "price": 0.64},
            "macro_f1_delta": 0.01,
        },
    )

    assert record["replayed"] is True
    assert record["tag_key_deltas"] == {"intent": 0.06, "price": -0.08}
    assert record["macro_f1_delta"] == 0.01


def test_a_tag_the_patch_silenced_entirely_shows_as_a_full_drop() -> None:
    """Dropping it from the diff would hide the worst side effect there is."""

    assert tag_key_deltas(before={"price": 0.7}, after={}) == {"price": -0.7}


def test_a_tag_the_patch_introduced_shows_as_a_gain_from_zero() -> None:
    assert tag_key_deltas(before={}, after={"price": 0.4}) == {"price": 0.4}


def test_unchanged_tags_are_left_out_of_the_side_effect_list() -> None:
    # The panel lists every entry; unchanged rows would bury the ones that moved.
    assert tag_key_deltas(before={"a": 0.5, "b": 0.5}, after={"a": 0.5, "b": 0.9}) == {"b": 0.4}


def test_deltas_are_ordered_so_two_identical_compiles_agree() -> None:
    deltas = tag_key_deltas(before={"z": 0.1, "a": 0.1}, after={"z": 0.2, "a": 0.3})

    assert list(deltas) == ["a", "z"]


# ------------------------------------------------------------------ persistence


def test_gradient_rows_carry_the_diagnosis_not_the_rationale() -> None:
    step = StubStep()
    artifact, proposer = _propose(step)
    clusters = cluster_badcases([_badcase(1)])

    (row,) = gradient_rows(
        artifact,
        proposer.gradients,
        {artifact.patches[0].patch_id: clusters},
    )

    assert row["gradient_text"] == "现行规则只认明确表述，未覆盖间接表达。"
    assert row["proposed_edit"] == _EDIT
    assert row["tag_key"] == "intent"
    assert row["failure_stage"] == "tag_reasoning"
    assert row["source_badcase_id"] == 1
    assert "gradient_text" not in row["evaluation"], "the diagnosis has its own column"


def test_a_row_with_no_diagnosis_falls_back_to_the_rationale() -> None:
    """Which says the template was used -- the only honest thing left to show."""

    artifact, proposer = _propose(StubStep(RuntimeError("boom")))

    (row,) = gradient_rows(artifact, proposer.gradients, {})

    assert "梯度步骤失败" in row["gradient_text"]
    assert row["failure_stage"] == "tag_reasoning"


@pytest.mark.parametrize("records", [{}, {"unknown": {}}])
def test_a_missing_record_does_not_lose_the_patch(records: dict[str, Any]) -> None:
    artifact, _ = _propose(StubStep())

    rows = gradient_rows(artifact, records, {})

    assert len(rows) == 1
    assert rows[0]["evaluation"] == {}


def test_a_replay_without_per_label_numbers_omits_the_side_effect_list() -> None:
    """The evaluation API does not always return per-label F1; inventing one is worse."""

    cluster = cluster_badcases([_badcase(1, occurrences=40)])[0]

    record = build_evaluation_record(
        cluster,
        rounds=1,
        replay={"macro_f1_delta": -0.02, "resolved_badcases": 3},
    )

    assert record["replayed"] is True
    assert "tag_key_deltas" not in record
    assert record["resolved_badcases"] == 3
