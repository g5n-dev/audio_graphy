"""Make a reviewed conversation safe to inline into a served prompt.

A demo is copied into an immutable ``TaggerVersion`` and sent to the provider on
every request thereafter. There is no route back out, so the redaction has to happen
before the artifact is built, not before it is displayed.

Two modes, and they are not interchangeable:

``masked``
    Direct identifiers are replaced in place. Cheap, deterministic, and it keeps the
    conversation's own wording -- which is what makes a demo teach anything.
``synthetic``
    A model rewrites the exchange into an equivalent fictional one, and the result is
    then *re-verified*: the baseline is run over the rewrite and its labels compared
    against the original truth. A rewrite that changes what the conversation means is
    discarded, because a demo that teaches the wrong answer is worse than no demo.

What this module deliberately does **not** mask
-----------------------------------------------
Amounts, discounts, models and trim levels stay. For most of the tag catalogue the
number *is* the signal -- masking it leaves a demo that says "some hidden figure
means ``price_discount=present``", which teaches nothing and quietly poisons the
few-shot block. Amounts are not direct identifiers; a conversation that is
identifying because of its numbers needs ``synthetic``, not a heavier mask.

Name matching is anchored, not general NER
------------------------------------------
``core.pii`` explicitly defers Chinese name recognition, and a bare surname+given
regex over Chinese text has dreadful precision ("王牌", "李子"). Names are matched
only next to an address term or a role word. That is precise but incomplete, and a
demo whose safety depends on catching *every* name must use ``synthetic``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from audio_graphy.core.pii import PII_CATEGORIES, PIIScrubber

logger = logging.getLogger(__name__)

#: ``verbatim`` exists in the model layer for debugging and is refused before
#: persistence; it is not a mode this module can produce.
REDACTION_MODES: tuple[str, ...] = ("masked", "synthetic")

_SCRUBBER = PIIScrubber()

# 中国大陆民用车牌：省份简称 + 发牌机关字母 + 5~6 位。新能源牌是 6 位。
_PLATE = re.compile(
    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领]"
    r"[A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}"
)

_SURNAME = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
)
_TITLE = "先生|女士|小姐|太太|总|经理|老师|师傅|主管"

#: Where a name ends when nothing on the right marks it. Chinese given names are one
#: or two characters, but without this the match runs on and eats the verb --
#: "客户张伟说" would redact to "客户<客户姓名>手机", quietly corrupting the demo.
_NAME_STOP = r"[\s，。、！？；：,.!?]|说|讲|表示|提到|问|想|要|留|来|去|是|的|把|给|跟|和"

#: Role words that begin with a surname character. Without this exclusion "销售顾问"
#: reads as role + surname 顾, and every consultant in the transcript gets redacted.
_ROLE_COLLISIONS = "顾问|经理|主管"

#: Anchored name shapes. Each needs either a title on the right or a role word on the
#: left, so ordinary vocabulary that happens to start with a surname ("王牌车型",
#: "李子园") is left alone. Both are verified against those cases in the tests.
_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 张先生 / 李女士 / 王总 / 陈经理 -- the title itself is the right boundary.
    re.compile(rf"[{_SURNAME}](?:[一-龥]{{1,2}})?(?={_TITLE})"),
    # 客户张三 / 顾问李四 -- lazy, and only closes on punctuation or a verb.
    re.compile(
        rf"(?<=客户|顾客|车主|销售|顾问)(?!{_ROLE_COLLISIONS})"
        rf"[{_SURNAME}][一-龥]{{0,2}}?(?={_NAME_STOP}|$)"
    ),
)

_PLATE_MASK = "<车牌>"
_NAME_MASK = "<客户姓名>"


class RedactionError(RuntimeError):
    """Raised when a demo cannot be made safe to inline."""


@dataclass(frozen=True, slots=True)
class RedactionOutcome:
    """The text a demo will actually carry, plus what had to be done to it."""

    text: str
    mode: str
    categories: tuple[str, ...]
    verified: bool = True

    def __post_init__(self) -> None:
        if self.mode not in REDACTION_MODES:
            raise RedactionError(f"unsupported redaction mode: {self.mode}")


class TextRewriter(Protocol):
    """Synchronous single-turn rewrite. Bound to a ``GatewayLM`` by the worker."""

    def complete_text(self, prompt: str, *, system: str | None = None) -> str: ...


_SYNTHESIS_SYSTEM = (
    "你是对话脱敏改写器。把用户给出的门店销售对话改写成一段虚构但等价的对话。"
    "要求：\n"
    "1. 完全替换所有真实身份信息（人名、电话、车牌、住址、门店名）为虚构的；\n"
    "2. 严格保留对话的判定信号——意向强弱、异议类型、优惠幅度、承诺与否都不能变；\n"
    "3. 保留原有的说话轮次与角色标记，不要合并或增删轮次；\n"
    "4. 只输出改写后的对话正文，不要解释、不要加标题。"
)


def mask_text(text: str) -> RedactionOutcome:
    """Replace direct identifiers in *text*, leaving the tagging signal intact."""

    if not text.strip():
        raise RedactionError("cannot redact empty demo text")

    scrubbed = _SCRUBBER.scrub(text, categories=PII_CATEGORIES)
    found = {record.category for record in scrubbed.redactions}
    body = scrubbed.text

    body, plates = _PLATE.subn(_PLATE_MASK, body)
    if plates:
        found.add("plate")

    for pattern in _NAME_PATTERNS:
        body, names = pattern.subn(_NAME_MASK, body)
        if names:
            found.add("person_name")

    return RedactionOutcome(text=body, mode="masked", categories=tuple(sorted(found)))


def truth_signature(truths: Sequence[Mapping[str, Any]]) -> frozenset[tuple[str, str]]:
    """The ``(tag_key, value)`` set a demo is supposed to demonstrate."""

    return frozenset(
        (str(truth.get("tag_key") or ""), str(truth.get("value") or ""))
        for truth in truths
        if truth.get("tag_key")
    )


def assignment_signature(assignments: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """The same shape, read out of a prediction result."""

    rows = assignments.get("assignments")
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        return frozenset()
    return frozenset(
        (str(row.get("tag_key") or ""), str(row.get("value") or ""))
        for row in rows
        if isinstance(row, Mapping) and row.get("tag_key")
    )


def synthesize_text(text: str, *, rewriter: TextRewriter) -> RedactionOutcome:
    """Ask the model for a fictional but signal-preserving rewrite, then mask it.

    Masking the rewrite is not belt-and-braces. A model told to invent a phone number
    will happily invent a real-looking one, and "the model promised it was fictional"
    is not a privacy control.

    The outcome carries the categories the mask actually hit, so ``verified`` is the
    only thing left for the caller to establish. Re-running :func:`mask_text` on the
    result to recover them would report nothing -- masking is idempotent, so the
    second pass finds an already-clean string.
    """

    if not text.strip():
        raise RedactionError("cannot redact empty demo text")

    rewritten = rewriter.complete_text(text, system=_SYNTHESIS_SYSTEM).strip()
    if not rewritten:
        raise RedactionError("the rewriter returned nothing")
    masked = mask_text(rewritten)
    return RedactionOutcome(
        text=masked.text,
        mode="synthetic",
        categories=masked.categories,
        verified=False,
    )


def verify_rewrite(
    *,
    truths: Sequence[Mapping[str, Any]],
    assignments: Mapping[str, Any],
) -> bool:
    """Did the rewrite keep the labels the original earned?

    Set equality, not a score: a rewrite that drops one label or invents another has
    changed what the conversation demonstrates, and inlining it would teach the model
    a judgement no reviewer ever made.
    """

    return truth_signature(truths) == assignment_signature(assignments)


async def redact_demo(
    text: str,
    *,
    mode: str,
    truths: Sequence[Mapping[str, Any]] = (),
    rewriter: TextRewriter | None = None,
    reverify: Any = None,
) -> RedactionOutcome | None:
    """Produce the text a demo will carry, or ``None`` if it cannot be made safe.

    *reverify* is an awaitable taking the rewritten text and returning predicted
    assignments -- the worker binds it to a real baseline run. Synthetic mode without
    it is refused rather than downgraded: an unverified rewrite is exactly the failure
    this mode exists to prevent.
    """

    if mode == "masked":
        return mask_text(text)
    if mode != "synthetic":
        raise RedactionError(f"unsupported redaction mode: {mode}")

    if rewriter is None:
        raise RedactionError("synthetic redaction needs a rewriter")
    if reverify is None:
        raise RedactionError(
            "synthetic redaction needs a re-verification step; an unverified rewrite "
            "may have changed the judgement the demo is meant to teach"
        )

    candidate = synthesize_text(text, rewriter=rewriter)
    assignments = await reverify(candidate.text)
    if not verify_rewrite(truths=truths, assignments=assignments):
        logger.info("discarding a synthetic demo whose labels no longer match the review")
        return None
    return replace(candidate, verified=True)
