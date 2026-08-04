"""Determinism guarantees that make human-in-the-loop patch review safe."""

from __future__ import annotations

import itertools

import pytest

from audio_graphy.optimizers.artifacts import (
    CompiledPromptArtifact,
    PromptArtifactError,
    PromptDemo,
    PromptPatch,
    artifact_from_payload,
    build_demo_id,
    build_patch_id,
    rematerialize,
)


def _patch(patch_id: str, *, ordinal: int, body: str) -> PromptPatch:
    return PromptPatch(
        patch_id=patch_id,
        kind="rule_clarification",
        origin="builtin",
        ordinal=ordinal,
        body=body,
        rationale=f"来自 {patch_id} 的诊断",
        target_tag_keys=("intent",),
    )


def _demo(demo_id: str, *, text: str) -> PromptDemo:
    return PromptDemo(
        demo_id=demo_id,
        gold_label_id=int(demo_id[-1]),
        subject_type="dialogue_unit",
        subject_id=int(demo_id[-1]),
        rendered_text=text,
        redaction_mode="synthetic",
        source_checksum="a" * 64,
        reception_id=7,
        segment_ids=(1, 2),
    )


def _artifact(**overrides: object) -> CompiledPromptArtifact:
    defaults: dict[str, object] = {
        "baseline_prompt": "基线规则",
        "header": "标签判定总则",
        "compiler": "builtin",
        "compiler_version": "builtin-v1",
        "metric_version": "prompt-lab-metric-v1",
        "patches": (
            _patch("p1", ordinal=1, body="规则一：出现明确金额才输出价格标签。"),
            _patch("p2", ordinal=2, body="规则二：跨句证据需同时引用两个 segment。"),
            _patch("p3", ordinal=3, body="规则三：不确定时省略。"),
        ),
        "demos": (_demo("d1", text="示例一"), _demo("d2", text="示例二")),
        "accepted_patch_ids": frozenset({"p1", "p2", "p3"}),
    }
    return CompiledPromptArtifact(**(defaults | overrides))  # type: ignore[arg-type]


def test_render_places_header_then_patches_by_ordinal_then_demos() -> None:
    rendered = _artifact().render()

    assert rendered.startswith("标签判定总则")
    assert rendered.index("规则一") < rendered.index("规则二") < rendered.index("规则三")
    assert rendered.index("规则三") < rendered.index("示例：")
    assert rendered.index("示例：") < rendered.index("示例一")


def test_render_order_ignores_the_order_patches_were_supplied_in() -> None:
    forward = _artifact()
    shuffled = _artifact(patches=tuple(reversed(forward.patches)))

    assert forward.render() == shuffled.render()
    assert forward.checksum() != shuffled.checksum(), (
        "checksum covers the full patch list, including rejected ones"
    )


_ALL_PATCH_IDS = ("p1", "p2", "p3")
_EVERY_SUBSET = [
    frozenset(combo)
    for size in range(len(_ALL_PATCH_IDS) + 1)
    for combo in itertools.combinations(_ALL_PATCH_IDS, size)
]


@pytest.mark.parametrize("accepted", _EVERY_SUBSET, ids=lambda s: "+".join(sorted(s)) or "none")
def test_rematerialize_is_idempotent_for_every_accepted_subset(accepted: frozenset[str]) -> None:
    """The property partial acceptance rests on: same decisions, same bytes."""

    parent = _artifact()

    first = rematerialize(parent, accepted_patch_ids=accepted)
    second = rematerialize(parent, accepted_patch_ids=accepted)

    assert first.render() == second.render()
    assert first.checksum() == second.checksum()


def test_rejecting_a_patch_removes_its_text_and_keeps_the_rest() -> None:
    child = rematerialize(_artifact(), accepted_patch_ids={"p1", "p3"})

    rendered = child.render()
    assert "规则一" in rendered
    assert "规则二" not in rendered
    assert "规则三" in rendered
    assert {patch.patch_id for patch in child.patches} == {"p1", "p3"}


def test_dropping_a_demo_removes_it_and_the_heading_when_none_remain() -> None:
    child = rematerialize(
        _artifact(),
        accepted_patch_ids={"p1"},
        dropped_demo_ids={"d1", "d2"},
    )

    rendered = child.render()
    assert "示例：" not in rendered
    assert child.demos == ()


def test_rejecting_everything_leaves_only_the_header() -> None:
    child = rematerialize(_artifact(), accepted_patch_ids=set(), dropped_demo_ids={"d1", "d2"})

    assert child.render() == "标签判定总则"


def test_a_stale_decision_is_rejected_rather_than_silently_dropped() -> None:
    with pytest.raises(PromptArtifactError, match="unknown patch_id"):
        rematerialize(_artifact(), accepted_patch_ids={"p1", "p999"})
    with pytest.raises(PromptArtifactError, match="unknown demo_id"):
        rematerialize(_artifact(), accepted_patch_ids={"p1"}, dropped_demo_ids={"d999"})


def test_duplicate_ids_are_refused_at_construction() -> None:
    with pytest.raises(PromptArtifactError, match="patch_id must be unique"):
        _artifact(patches=(_patch("p1", ordinal=1, body="甲"), _patch("p1", ordinal=2, body="乙")))
    with pytest.raises(PromptArtifactError, match="demo_id must be unique"):
        _artifact(demos=(_demo("d1", text="甲"), _demo("d1", text="乙")))


def test_accepting_a_patch_the_artifact_does_not_contain_is_refused() -> None:
    with pytest.raises(PromptArtifactError, match="reference unknown patches"):
        _artifact(accepted_patch_ids=frozenset({"p1", "nope"}))


def test_payload_round_trip_preserves_render_and_checksum() -> None:
    original = rematerialize(_artifact(), accepted_patch_ids={"p1", "p3"})

    restored = artifact_from_payload(original.as_payload())

    assert restored.render() == original.render()
    assert restored.checksum() == original.checksum()


def test_content_addressed_ids_are_stable_and_discriminating() -> None:
    first = build_patch_id(origin="builtin", body=" 规则一 ", target_tag_keys=["intent"])
    same = build_patch_id(origin="builtin", body="规则一", target_tag_keys=["intent"])
    other = build_patch_id(origin="builtin", body="规则二", target_tag_keys=["intent"])

    assert first == same, "surrounding whitespace must not mint a new identity"
    assert first != other
    assert len(first) == 32

    demo = build_demo_id(subject_type="dialogue_unit", subject_id=1, rendered_text="示例")
    assert demo == build_demo_id(subject_type="dialogue_unit", subject_id=1, rendered_text=" 示例 ")
    assert demo != build_demo_id(subject_type="reception", subject_id=1, rendered_text="示例")


def test_token_estimate_grows_with_accepted_content() -> None:
    full = _artifact()
    trimmed = rematerialize(full, accepted_patch_ids={"p1"}, dropped_demo_ids={"d1", "d2"})

    assert trimmed.prompt_token_estimate < full.prompt_token_estimate


def test_each_part_reports_its_own_token_cost() -> None:
    """The diff view prices patches and demos individually, not just the whole prompt."""

    artifact = _artifact()

    assert all(patch.prompt_token_estimate > 0 for patch in artifact.patches)
    assert all(demo.prompt_token_estimate > 0 for demo in artifact.demos)
    long_patch = _patch("p9", ordinal=9, body="规则" * 200)
    assert long_patch.prompt_token_estimate > artifact.patches[0].prompt_token_estimate


def test_an_empty_header_does_not_leave_a_leading_blank_block() -> None:
    rendered = _artifact(header="   ").render()

    assert rendered.startswith("规则一")
