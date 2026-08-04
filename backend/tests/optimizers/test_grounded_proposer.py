"""Grounded instruction proposal without DSPy.

The point of this proposer is that a model writes the rule instead of a template.
The point of its *validation* is that a model is allowed to be useless: anything it
returns that cannot be trusted degrades to the deterministic rule ``BuiltinProposer``
would have produced, because a compile that quietly ships a rule contradicting the
baseline contract is worse than one that ships a dull rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.optimizers.proposers import (
    GROUNDED_COMPILER_VERSION,
    BuiltinGroundedProposer,
    ProposalRequest,
    cluster_badcases,
    sanitize_instruction,
)

_DEFINITIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "key": "intent",
        "description": "客户的购车意向阶段。",
        "value_type": "enum",
        "allowed_values": ["purchase", "browse"],
    },
}


class StubWriter:
    def __init__(
        self, output: str | Exception = "标签 intent 判定时应以客户明确表述为准。"
    ) -> None:
        self.output = output
        self.prompts: list[str] = []
        self.systems: list[str | None] = []

    def complete_text(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _badcase(badcase_id: int, *, occurrences: int = 6, tag_key: str = "intent") -> dict[str, Any]:
    return {
        "id": badcase_id,
        "tag_key": tag_key,
        "failure_stage": "tag_reasoning",
        "occurrence_count": occurrences,
        "root_cause": {"reason_code": "missed_label", "truth_state": "present"},
    }


def _propose(writer: StubWriter, rows: list[dict[str, Any]] | None = None) -> Any:
    return BuiltinGroundedProposer(writer=writer).propose(
        ProposalRequest(
            baseline_prompt="基线规则：按 schema 判定标签。",
            clusters=cluster_badcases(rows if rows is not None else [_badcase(1)]),
            definitions=_DEFINITIONS,
        )
    )


# ------------------------------------------------------------------- sanitising


def test_a_usable_rule_passes_through() -> None:
    assert sanitize_instruction("标签 intent 需要直接文本依据。", tag_key="intent")


@pytest.mark.parametrize(
    "raw",
    [
        "```\n标签 intent 需要直接文本依据。\n```",
        "- 标签 intent 需要直接文本依据。",
        "1. 标签 intent 需要直接文本依据。",
        '"标签 intent 需要直接文本依据。"',
        "“标签 intent 需要直接文本依据。”",
    ],
)
def test_the_decorations_models_add_despite_instructions_are_stripped(raw: str) -> None:
    assert sanitize_instruction(raw, tag_key="intent") == "标签 intent 需要直接文本依据。"


@pytest.mark.parametrize(
    "raw",
    [
        "标签 intent 拿不准时可以猜测一个取值。",
        "标签 intent 即使没有证据也应输出。",
        "标签 intent 宁可多标不可漏标。",
    ],
)
def test_a_rule_that_would_cancel_the_baseline_contract_is_rejected(raw: str) -> None:
    """The system prompt already says to omit unsupported labels; both cannot hold."""

    assert sanitize_instruction(raw, tag_key="intent") is None


def test_a_rule_that_never_names_its_tag_is_rejected() -> None:
    # Otherwise the diff view shows an edit that cannot be tied back to any failure.
    assert sanitize_instruction("判定时请更谨慎一些。", tag_key="intent") is None


def test_an_overlong_rule_is_rejected_rather_than_truncated() -> None:
    # Truncating could cut mid-clause and invert the rule's meaning.
    assert sanitize_instruction("标签 intent " + "很长的说明。" * 60, tag_key="intent") is None


def test_an_empty_rule_is_rejected() -> None:
    assert sanitize_instruction("```\n\n```", tag_key="intent") is None


# -------------------------------------------------------------------- proposing


def test_the_model_written_rule_becomes_the_patch_body() -> None:
    artifact = _propose(StubWriter("标签 intent 只在客户明确表态时判定为 purchase。"))

    (patch,) = artifact.patches
    assert patch.body == "标签 intent 只在客户明确表态时判定为 purchase。"
    assert "由模型" in patch.rationale


def test_the_meta_prompt_carries_the_evidence_and_the_current_rule() -> None:
    writer = StubWriter()

    _propose(writer)

    prompt = writer.prompts[0]
    assert "基线规则：按 schema 判定标签。" in prompt
    assert "intent" in prompt
    assert "missed_label" in prompt
    assert "purchase、browse" in prompt


def test_the_system_prompt_forbids_guessing() -> None:
    writer = StubWriter()

    _propose(writer)

    assert "猜测" in (writer.systems[0] or "")


def test_a_rejected_rule_degrades_to_the_template_not_to_nothing() -> None:
    artifact = _propose(StubWriter("随便写点什么。"))

    (patch,) = artifact.patches
    assert "取值判错" in patch.body, "the deterministic template must still be there"
    assert "purchase、browse" in patch.body
    assert "未通过校验" in patch.rationale


def test_a_provider_failure_degrades_to_the_template() -> None:
    """Losing the cluster entirely would drop evidence a reviewer already produced."""

    artifact = _propose(StubWriter(RuntimeError("provider timeout")))

    (patch,) = artifact.patches
    assert "取值判错" in patch.body
    assert "提案失败" in patch.rationale


def test_one_call_is_made_per_eligible_cluster_and_no_more() -> None:
    writer = StubWriter()

    _propose(
        writer,
        [
            _badcase(1, tag_key="intent", occurrences=6),
            _badcase(2, tag_key="intent", occurrences=6),
            # Below the support threshold: it must not cost a provider call.
            _badcase(3, tag_key="price_discount", occurrences=1),
        ],
    )

    assert len(writer.prompts) == 1, "same cluster key collapses into one call"


def test_the_patch_cap_bounds_the_number_of_calls() -> None:
    writer = StubWriter()
    rows = [
        {
            "id": index,
            "tag_key": "intent",
            "failure_stage": "tag_reasoning",
            "cluster_key": f"c{index}",
            "occurrence_count": 6,
            "root_cause": {"reason_code": "missed_label", "truth_state": "present"},
        }
        for index in range(1, 10)
    ]
    BuiltinGroundedProposer(writer=writer).propose(
        ProposalRequest(
            baseline_prompt="基线",
            clusters=cluster_badcases(rows),
            definitions=_DEFINITIONS,
            max_patches=3,
        )
    )

    assert len(writer.prompts) == 3


def test_the_artifact_names_the_compiler_that_actually_ran() -> None:
    # Not "builtin" with a telltale version string: one compiler spends provider
    # budget and one does not, and an artifact that blurs them makes every later
    # comparison between compilers read fabricated data.
    artifact = _propose(StubWriter())

    assert artifact.compiler == "builtin_grounded"
    assert artifact.compiler_version == GROUNDED_COMPILER_VERSION
    assert all(patch.origin == "builtin_grounded" for patch in artifact.patches)


def test_the_baseline_policy_is_still_kept_verbatim() -> None:
    artifact = _propose(StubWriter())

    assert artifact.render().startswith("基线规则：按 schema 判定标签。")


def test_two_identical_requests_produce_the_same_patch_ids() -> None:
    first = _propose(StubWriter())
    second = _propose(StubWriter())

    assert [patch.patch_id for patch in first.patches] == [
        patch.patch_id for patch in second.patches
    ]


def test_no_eligible_cluster_means_no_calls_and_no_patches() -> None:
    writer = StubWriter()

    artifact = _propose(writer, [_badcase(1, occurrences=1)])

    assert artifact.patches == ()
    assert writer.prompts == []


def test_a_tag_without_a_value_list_still_gets_a_meta_prompt() -> None:
    writer = StubWriter("标签 price_discount 需要客户明确提及折扣。")

    BuiltinGroundedProposer(writer=writer).propose(
        ProposalRequest(
            baseline_prompt="基线",
            clusters=cluster_badcases([_badcase(1, tag_key="price_discount")]),
            definitions={"price_discount": {"key": "price_discount", "value_type": "string"}},
        )
    )

    assert "合法取值" not in writer.prompts[0]
