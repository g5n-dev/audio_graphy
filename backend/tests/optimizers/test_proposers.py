"""The model-free baseline proposer: deterministic, honest, contract-respecting."""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.optimizers.proposers import (
    BuiltinProposer,
    ProposalRequest,
    cluster_badcases,
)

_DEFINITIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "key": "intent",
        "value_type": "enum",
        "allowed_values": ["purchase", "browse"],
    },
    "price_discount": {"key": "price_discount", "value_type": "string"},
}


def _badcase(
    badcase_id: int,
    *,
    tag_key: str = "intent",
    stage: str = "tag_reasoning",
    reason: str = "missed_label",
    truth_state: str = "present",
    occurrences: int = 1,
    upstream: bool = False,
) -> dict[str, Any]:
    return {
        "id": badcase_id,
        "tag_key": tag_key,
        "failure_stage": stage,
        "cluster_key": f"{stage}:{tag_key}:{reason}",
        "occurrence_count": occurrences,
        "root_cause": {
            "reason_code": reason,
            "truth_state": truth_state,
            "upstream_routed": upstream,
        },
    }


def test_upstream_failures_never_become_prompt_advice() -> None:
    """A bad transcript is not a prompt problem; teaching the prompt about it is noise."""

    rows = [
        _badcase(1, stage="asr", reason="garbled_audio"),
        _badcase(2, stage="vad", reason="clipped_turn"),
        _badcase(3, stage="speaker", reason="wrong_diarization"),
        _badcase(4, stage="tag_reasoning", reason="missed_label"),
    ]

    clusters = cluster_badcases(rows)

    assert [cluster.failure_stage for cluster in clusters] == ["tag_reasoning"]


def test_upstream_routed_flag_is_honoured_even_when_the_stage_looks_fixable() -> None:
    rows = [_badcase(1, stage="tag_reasoning", upstream=True)]

    assert cluster_badcases(rows) == ()


def test_clusters_are_ordered_by_support_then_key() -> None:
    rows = [
        _badcase(1, tag_key="price_discount", reason="spurious", occurrences=2),
        _badcase(2, tag_key="intent", reason="missed_label", occurrences=9),
        _badcase(3, tag_key="intent", reason="missed_label", occurrences=1),
    ]

    clusters = cluster_badcases(rows)

    assert [cluster.tag_key for cluster in clusters] == ["intent", "price_discount"]
    assert clusters[0].occurrence_count == 10
    assert clusters[0].badcase_ids == (2, 3)


def test_clustering_is_independent_of_row_order() -> None:
    rows = [
        _badcase(1, tag_key="intent", occurrences=4),
        _badcase(2, tag_key="price_discount", reason="spurious", occurrences=4),
    ]

    assert cluster_badcases(rows) == cluster_badcases(list(reversed(rows)))


def _propose(rows: list[dict[str, Any]], **kwargs: Any) -> Any:
    return BuiltinProposer().propose(
        ProposalRequest(
            baseline_prompt="基线规则：按 schema 判定标签。",
            clusters=cluster_badcases(rows),
            definitions=_DEFINITIONS,
            **kwargs,
        )
    )


def test_clusters_below_the_support_threshold_are_ignored() -> None:
    artifact = _propose([_badcase(1, occurrences=2)], min_cluster_support=3)

    assert artifact.patches == ()
    assert artifact.render() == "基线规则：按 schema 判定标签。"


def test_a_missed_label_cluster_produces_a_detection_rule() -> None:
    artifact = _propose([_badcase(1, occurrences=7, truth_state="present")])

    (patch,) = artifact.patches
    assert "漏判" in patch.body or "取值判错" in patch.body
    assert "7" in patch.body
    assert patch.target_tag_keys == ("intent",)
    assert patch.source_badcase_ids == (1,)


def test_a_spurious_label_cluster_produces_an_omission_rule() -> None:
    artifact = _propose(
        [_badcase(1, tag_key="price_discount", truth_state="absent", occurrences=5)]
    )

    (patch,) = artifact.patches
    assert "误判为成立" in patch.body
    assert "省略" in patch.body


def test_an_enum_tag_gets_its_allowed_values_spelled_out() -> None:
    artifact = _propose([_badcase(1, tag_key="intent", occurrences=6)])

    (patch,) = artifact.patches
    assert "purchase" in patch.body and "browse" in patch.body


def test_evidence_and_schema_failures_get_their_own_templates() -> None:
    artifact = _propose(
        [
            _badcase(1, stage="evidence", reason="wrong_segment", occurrences=5),
            _badcase(2, stage="schema", reason="extra_field", occurrences=4),
        ]
    )

    bodies = [patch.body for patch in artifact.patches]
    assert len(artifact.patches) == 2
    assert {patch.kind for patch in artifact.patches} == {"constraint_add"}
    assert any("evidence_segment_ids" in body for body in bodies)
    assert any("response schema" in body for body in bodies)


def test_generated_rules_never_contradict_the_stable_contract() -> None:
    """The contract already says to omit unsupported labels; patches must not fight it."""

    artifact = _propose(
        [
            _badcase(1, tag_key="intent", truth_state="present", occurrences=6),
            _badcase(2, tag_key="price_discount", truth_state="absent", occurrences=6),
            _badcase(3, stage="evidence", reason="wrong_segment", occurrences=6),
        ]
    )

    rendered = artifact.render()
    for forbidden in ("猜测", "宁可", "即使没有证据", "全部输出"):
        assert forbidden not in rendered


def test_proposal_is_capped_and_ordinals_are_contiguous() -> None:
    rows = [
        _badcase(index, tag_key=f"tag_{index}", occurrences=20 - index) for index in range(1, 12)
    ]

    artifact = _propose(rows, max_patches=4)

    assert len(artifact.patches) == 4
    assert [patch.ordinal for patch in artifact.patches] == [1, 2, 3, 4]
    assert artifact.accepted_patch_ids == {patch.patch_id for patch in artifact.patches}


def test_recompiling_the_same_badcases_reproduces_the_same_artifact() -> None:
    rows = [
        _badcase(1, tag_key="intent", occurrences=6),
        _badcase(2, tag_key="price_discount", truth_state="absent", occurrences=4),
    ]

    first = _propose(rows)
    second = _propose(list(reversed(rows)))

    assert first.checksum() == second.checksum()
    assert first.render() == second.render()


def test_a_malformed_allowed_values_falls_back_to_the_generic_rule() -> None:
    """Definitions come from a JSON column, so a bad shape must not crash a compile."""

    artifact = BuiltinProposer().propose(
        ProposalRequest(
            baseline_prompt="基线规则",
            clusters=cluster_badcases([_badcase(1, tag_key="intent", occurrences=6)]),
            definitions={"intent": {"key": "intent", "allowed_values": "purchase"}},
        )
    )

    (patch,) = artifact.patches
    assert "purchase" not in patch.body
    assert "漏判" in patch.body


def test_the_baseline_policy_survives_verbatim_in_the_rendered_prompt() -> None:
    artifact = _propose([_badcase(1, occurrences=6)])

    assert artifact.render().startswith("基线规则：按 schema 判定标签。")
    assert artifact.baseline_prompt == "基线规则：按 schema 判定标签。"


def test_only_compilers_with_an_implementation_are_accepted() -> None:
    """CompilerName in the schema is wider than what this build can run."""

    from audio_graphy.optimizers.proposers import (
        IMPLEMENTED_COMPILERS,
        UnsupportedCompilerError,
        assert_compiler_supported,
    )

    for name in sorted(IMPLEMENTED_COMPILERS):
        assert_compiler_supported(name)

    # DSPy-native compilers are named in the schema and have no implementation yet.
    with pytest.raises(UnsupportedCompilerError) as caught:
        assert_compiler_supported("dspy_mipro")

    # 报错要说清可用的是什么，否则用户只知道失败、不知道该改成什么。
    message = str(caught.value)
    assert "dspy_mipro" in message
    assert all(name in message for name in IMPLEMENTED_COMPILERS)
