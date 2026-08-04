"""What a demo carries once it is safe to inline.

A demo ends up copied into an immutable TaggerVersion and sent to the provider on
every request. Two ways that goes wrong, and both are tested here: an identifier
that survives is a privacy failure, and an over-eager mask that eats the sentence is
a demo that teaches nonsense.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from audio_graphy.optimizers.redaction import (
    RedactionError,
    RedactionOutcome,
    assignment_signature,
    mask_text,
    redact_demo,
    synthesize_text,
    truth_signature,
    verify_rewrite,
)


class StubRewriter:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompts: list[str] = []
        self.systems: list[str | None] = []

    def complete_text(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        return self.output


def _truths(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"tag_key": key, "value": value} for key, value in pairs]


def _assignments(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"assignments": [{"tag_key": key, "value": value} for key, value in pairs]}


# ------------------------------------------------------------------- identifiers


@pytest.mark.parametrize(
    ("text", "must_not_contain", "category"),
    [
        ("手机 13800138000 联系我", "13800138000", "phone"),
        ("邮箱 li.mei@example.com", "li.mei@example.com", "email"),
        ("看的是京A12345 那台", "京A12345", "plate"),
        ("新能源牌 粤BD12345 已上", "粤BD12345", "plate"),
    ],
)
def test_a_direct_identifier_does_not_survive(
    text: str, must_not_contain: str, category: str
) -> None:
    outcome = mask_text(text)

    assert must_not_contain not in outcome.text
    assert category in outcome.categories


@pytest.mark.parametrize(
    "text",
    [
        "张先生想要试驾",
        "王总表示优惠可以谈",
        "李经理来看车",
        "客户张伟说手机号是多少",
        "顾客李梅留了邮箱",
        "车主陈国强的车",
    ],
)
def test_an_anchored_name_is_redacted(text: str) -> None:
    outcome = mask_text(text)

    assert "person_name" in outcome.categories
    assert "<客户姓名>" in outcome.text


@pytest.mark.parametrize(
    "text",
    [
        "这台车是王牌车型",
        "李子园附近的门店",
        "销售顾问跟进中",
        "销售经理来了",
    ],
)
def test_ordinary_vocabulary_starting_with_a_surname_is_left_alone(text: str) -> None:
    """Over-masking is not the safe direction: it silently corrupts the demo."""

    assert mask_text(text).text == text


def test_the_verb_after_a_name_survives_the_mask() -> None:
    # A greedy match here would produce "客户<客户姓名>手机号是多少" -- the demo would
    # read as if the customer never said anything.
    assert "说手机号" in mask_text("客户张伟说手机号是多少").text


# ----------------------------------------------------------------- what stays put


@pytest.mark.parametrize(
    "text",
    [
        "优惠 3 万可以签",
        "落地价 38.6 万",
        "这台是 2024 款豪华版",
        "赠送保养 5 次",
    ],
)
def test_the_tagging_signal_is_not_masked(text: str) -> None:
    """The number is usually the thing the tag turns on; masking it teaches nothing."""

    assert mask_text(text).text == text


def test_masking_is_reported_per_category() -> None:
    outcome = mask_text("客户张伟的手机 13800138000，车牌 京A12345")

    assert set(outcome.categories) == {"person_name", "phone", "plate"}
    assert outcome.mode == "masked"
    assert outcome.verified is True


def test_masking_an_already_masked_demo_changes_nothing_further() -> None:
    once = mask_text("客户张伟 13800138000 京A12345")

    assert mask_text(once.text).text == once.text


def test_empty_text_is_refused_rather_than_stored_as_a_blank_demo() -> None:
    with pytest.raises(RedactionError, match="empty"):
        mask_text("   ")


# ------------------------------------------------------------------- signatures


def test_a_truth_set_and_a_matching_prediction_agree() -> None:
    assert verify_rewrite(
        truths=_truths(("intent", "purchase"), ("price_discount", "present")),
        assignments=_assignments(("price_discount", "present"), ("intent", "purchase")),
    )


@pytest.mark.parametrize(
    "assignments",
    [
        _assignments(("intent", "browse")),
        _assignments(("intent", "purchase"), ("extra", "invented")),
        _assignments(),
        {"assignments": "not-a-list"},
        {},
    ],
)
def test_a_rewrite_that_changed_the_judgement_is_rejected(assignments: Mapping[str, Any]) -> None:
    assert not verify_rewrite(truths=_truths(("intent", "purchase")), assignments=assignments)


def test_rows_without_a_tag_key_are_ignored_rather_than_counted_as_empty_labels() -> None:
    assert truth_signature([{"value": "purchase"}]) == frozenset()
    assert assignment_signature({"assignments": [{"value": "purchase"}, "junk"]}) == frozenset()


# --------------------------------------------------------------------- synthetic


def test_a_rewrite_is_masked_even_though_it_claims_to_be_fictional() -> None:
    """ "The model said it invented the number" is not a privacy control."""

    rewriter = StubRewriter("客户王强说，联系电话 13900139000。")

    outcome = synthesize_text("原始对话", rewriter=rewriter)

    assert "13900139000" not in outcome.text
    assert "<客户姓名>" in outcome.text
    # 这里最容易写错：对已脱敏文本再跑一遍 mask_text 来取类别，结果恒为空。
    assert set(outcome.categories) == {"person_name", "phone"}
    assert outcome.verified is False, "尚未回放验证前不能自称已验证"


def test_the_rewrite_instruction_demands_the_judgement_be_preserved() -> None:
    rewriter = StubRewriter("改写结果")

    synthesize_text("原始对话", rewriter=rewriter)

    system = rewriter.systems[0] or ""
    assert "判定信号" in system
    assert "虚构" in system


def test_an_empty_rewrite_is_an_error_not_an_empty_demo() -> None:
    with pytest.raises(RedactionError, match="returned nothing"):
        synthesize_text("原始对话", rewriter=StubRewriter("   "))


@pytest.mark.asyncio
async def test_a_verified_synthetic_demo_is_kept() -> None:
    async def reverify(_: str) -> Mapping[str, Any]:
        return _assignments(("intent", "purchase"))

    outcome = await redact_demo(
        "客户张伟说想买",
        mode="synthetic",
        truths=_truths(("intent", "purchase")),
        rewriter=StubRewriter("顾客李强表示想买"),
        reverify=reverify,
    )

    assert outcome is not None
    assert outcome.mode == "synthetic"
    assert outcome.verified is True


@pytest.mark.asyncio
async def test_a_rewrite_that_no_longer_earns_its_labels_is_discarded() -> None:
    """Keeping it would teach a judgement no reviewer ever made."""

    async def reverify(_: str) -> Mapping[str, Any]:
        return _assignments(("intent", "browse"))

    outcome = await redact_demo(
        "客户张伟说想买",
        mode="synthetic",
        truths=_truths(("intent", "purchase")),
        rewriter=StubRewriter("顾客李强只是随便看看"),
        reverify=reverify,
    )

    assert outcome is None


@pytest.mark.asyncio
async def test_synthetic_mode_without_re_verification_is_refused_not_downgraded() -> None:
    with pytest.raises(RedactionError, match="re-verification"):
        await redact_demo(
            "客户张伟说想买",
            mode="synthetic",
            truths=_truths(("intent", "purchase")),
            rewriter=StubRewriter("改写结果"),
        )


@pytest.mark.asyncio
async def test_synthetic_mode_without_a_rewriter_is_refused() -> None:
    async def reverify(_: str) -> Mapping[str, Any]:
        return _assignments()

    with pytest.raises(RedactionError, match="rewriter"):
        await redact_demo("原始对话", mode="synthetic", reverify=reverify)


@pytest.mark.asyncio
async def test_masked_mode_needs_neither_a_model_nor_a_replay() -> None:
    outcome = await redact_demo("客户张伟说 13800138000", mode="masked")

    assert outcome is not None
    assert outcome.mode == "masked"


@pytest.mark.asyncio
async def test_verbatim_cannot_be_produced_here_at_all() -> None:
    # The model layer keeps the value for debugging; this module must not emit it.
    with pytest.raises(RedactionError, match="unsupported redaction mode"):
        await redact_demo("原始对话", mode="verbatim")


def test_an_outcome_cannot_claim_an_unsupported_mode() -> None:
    with pytest.raises(RedactionError, match="unsupported redaction mode"):
        RedactionOutcome(text="x", mode="verbatim", categories=())


def test_a_truth_sequence_type_that_is_not_a_list_is_handled() -> None:
    rows: Sequence[Mapping[str, Any]] = ({"tag_key": "intent", "value": "purchase"},)

    assert truth_signature(rows) == frozenset({("intent", "purchase")})


def test_synthesising_from_empty_text_is_refused_before_the_model_is_called() -> None:
    rewriter = StubRewriter("改写结果")

    with pytest.raises(RedactionError, match="empty"):
        synthesize_text("   ", rewriter=rewriter)

    assert rewriter.prompts == []
