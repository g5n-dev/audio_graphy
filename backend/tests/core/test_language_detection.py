"""Deterministic language and semantic-identifier guards."""

from audio_graphy.core.language_detection import (
    detect_semantic_language,
    semantic_protected_identifiers,
)


def test_detect_semantic_language_is_deterministic() -> None:
    assert detect_semantic_language("订单 SKUABC") == "zh-CN"
    assert detect_semantic_language("refund status") == "en"
    assert detect_semantic_language("123-456") == "und"


def test_semantic_protected_identifiers_cover_pure_letter_business_ids() -> None:
    values = semantic_protected_identifiers(
        "查询 SKUABC，order id: alpha，以及 user@example.com 和普通中文"
    )

    assert values == ("SKUABC", "alpha", "user@example.com")
