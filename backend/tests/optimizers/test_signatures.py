"""The signature description DSPy renders into the prompt.

Everything here ends up as prompt text a provider is billed for, so the two things
worth pinning are that it stays derived from the tenant's own definitions and that
it stays deterministic -- an unstable ordering would give two identical compiles
different prompts and therefore different artifact checksums.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.optimizers.signatures import (
    SignatureSpecError,
    build_tagging_signature,
    describe_tag,
)

_DEFINITIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "key": "intent",
        "description": "客户的购车意向阶段。",
        "value_type": "enum",
        "allowed_values": ["purchase", "browse"],
    },
    "price_discount": {"key": "price_discount", "value_type": "string"},
}


def _spec(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "instructions": "按标签定义判定对话",
        "definitions": _DEFINITIONS,
    }
    return build_tagging_signature(**(defaults | overrides))


# ------------------------------------------------------------------ describe_tag


def test_a_definition_contributes_both_its_meaning_and_its_value_space() -> None:
    described = describe_tag("intent", _DEFINITIONS["intent"])

    assert "客户的购车意向阶段。" in described
    assert "purchase、browse" in described


def test_a_definition_without_values_falls_back_to_its_type() -> None:
    assert "取值类型：string" in describe_tag("price_discount", _DEFINITIONS["price_discount"])


def test_a_missing_definition_still_names_the_tag() -> None:
    """A compile must not crash because one tag row is malformed."""

    assert describe_tag("mystery", None) == "标签 mystery。"


def test_a_string_valued_allowed_values_is_not_spelled_out_letter_by_letter() -> None:
    # A str is a Sequence; iterating one would put "p、u、r、c、h、a、s、e" in the prompt.
    described = describe_tag("intent", {"allowed_values": "purchase"})

    assert "p、u、r" not in described


def test_an_empty_value_list_does_not_produce_a_dangling_sentence() -> None:
    described = describe_tag("intent", {"description": "意向。", "allowed_values": []})

    assert "合法取值" not in described


def test_a_long_definition_is_cut_rather_than_pushing_the_prompt_over_budget() -> None:
    described = describe_tag("intent", {"description": "很长的说明。" * 200})

    assert len(described) <= 240
    assert described.endswith("…")


def test_whitespace_in_a_definition_is_collapsed() -> None:
    described = describe_tag("intent", {"description": "第一句。\n\n  第二句。"})

    assert "\n" not in described
    assert "第一句。 第二句。" in described


# ------------------------------------------------------------------- signature


def test_the_catalogue_lists_every_defined_tag_in_a_stable_order() -> None:
    spec = _spec()

    catalogue = spec.inputs[1].description
    assert catalogue.index("intent") < catalogue.index("price_discount")


def test_targeting_narrows_the_signature_to_the_tags_a_compile_is_about() -> None:
    spec = _spec(target_tag_keys=["price_discount"])

    catalogue = spec.inputs[1].description
    assert "price_discount" in catalogue
    assert "intent" not in catalogue


def test_asking_for_an_undefined_tag_is_refused() -> None:
    """Otherwise the model invents both the meaning and the value space."""

    with pytest.raises(SignatureSpecError, match="mystery"):
        _spec(target_tag_keys=["mystery"])


def test_a_signature_needs_at_least_one_definition() -> None:
    with pytest.raises(SignatureSpecError, match="at least one"):
        _spec(definitions={})


def test_empty_instructions_are_refused() -> None:
    with pytest.raises(SignatureSpecError, match="instructions"):
        _spec(instructions="   ")


def test_the_output_field_repeats_the_omit_unsupported_contract() -> None:
    # The baseline system prompt already says this; a signature that stayed silent
    # would leave the two halves of the prompt disagreeing.
    spec = _spec()

    assert "省略" in spec.outputs[0].description
    assert spec.outputs[0].annotation == "list[dict]"


def test_the_spec_serialises_for_replay() -> None:
    payload = _spec().as_mapping()

    assert payload["name"] == "TagDialogueUnit"
    assert [field["name"] for field in payload["inputs"]] == ["dialogue", "tag_catalogue"]
    assert [field["name"] for field in payload["outputs"]] == ["assignments"]


def test_two_identical_requests_describe_the_same_signature() -> None:
    assert _spec().as_mapping() == _spec().as_mapping()
