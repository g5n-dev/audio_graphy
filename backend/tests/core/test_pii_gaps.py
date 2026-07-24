"""Coverage gap-fill tests for PIIScrubber.

Targets uncovered branches in _mask helpers:
- empty input (returns ScrubResult with empty redactions list)
- landline phone (non-11-digit digit count)
- short-string keep-first/last fallback
- digit-less category fallback
- multi-octet IPv4 masking
- email with short local part (length<=1)
- redaction_char must be single char (ValueError)
- id_card with trailing X (preserve)
- id_card all-digit (different branch)
- custom categories subset
"""

from __future__ import annotations

import pytest

from audio_graphy.core.pii import PII_CATEGORIES, PIIScrubber


def test_empty_text_returns_empty_redactions() -> None:
    """scrub('') short-circuits to ScrubResult(text='', redactions=[])."""
    s = PIIScrubber()
    result = s.scrub("")
    assert result.text == ""
    assert result.redactions == []


def test_landline_masking_uses_keep_ends() -> None:
    """Landline (10-11 digits) → keep-first=3, keep-last=4 branch."""
    s = PIIScrubber()
    text = "call 010-12345678 please"
    result = s.scrub(text)
    # Landline pattern hit; replacement contains first 3 + last 4 digits.
    recs = [r for r in result.redactions if r.category == "phone"]
    assert len(recs) == 1
    # Replacement text on the source span (digits 01012345678).
    masked = result.text.split("call ")[1].split(" please")[0]
    assert masked.startswith("010")
    assert masked.endswith("5678")
    assert "*" in masked


def test_short_id_card_falls_back_to_full_mask() -> None:
    """A short ID-card-like string triggers the too-short fallback in _mask_keep_ends."""
    s = PIIScrubber()
    # 17-digit number can't be a real id_card (needs 18), so trigger via custom
    # constructed case where digits ≤ keep_first + keep_last.
    # bank_card keep_first=2 + keep_last=1 = 3; an exactly-3-digit bank-like
    # span isn't a valid bank card; instead exercise the fallback via a
    # tight id_card variant where digits length ≤ 4.
    # Use an explicit call to _mask_keep_ends to cover the branch.
    out = s._mask_keep_ends("ab", keep_first=2, keep_last=2, mask_len=4)
    # Too short → all masked.
    assert set(out) == {"*"}
    assert len(out) >= 2  # at least len("ab")


def test_digits_less_category_falls_back() -> None:
    """When digits parse empty but text matched, fallback mask is returned."""
    s = PIIScrubber()
    # _mask("phone", "") should return 4 stars via the "if not digits" branch.
    out = s._mask("phone", "")
    assert set(out) == {"*"}
    assert len(out) >= 4


def test_ipv4_masks_last_two_octets() -> None:
    """IPv4 mask preserves first 2 octets, masks last 2."""
    s = PIIScrubber()
    text = "server at 10.20.30.40 ok"
    result = s.scrub(text)
    masked = result.text.split("server at ")[1].split(" ok")[0]
    parts = masked.split(".")
    assert parts[0] == "10"
    assert parts[1] == "20"
    # Last two octets replaced by "**" each.
    assert parts[2] == "**"
    assert parts[3] == "**"


def test_email_short_local_part_branch() -> None:
    """Email with single-char local part triggers the at_idx<=1 branch."""
    s = PIIScrubber()
    text = "contact a@x.io for info"
    result = s.scrub(text)
    masked = result.text.split("contact ")[1].split(" for info")[0]
    # a@x.io → "a***@x.io" (single char kept, 3 stars, domain).
    assert masked.startswith("a")
    assert "***" in masked
    assert masked.endswith("@x.io")


def test_invalid_redaction_char_length_rejected() -> None:
    """redaction_char must be exactly 1 character."""
    with pytest.raises(ValueError, match="single character"):
        PIIScrubber(redaction_char="**")


def test_id_card_with_trailing_x_preserved() -> None:
    """id_card ending in X preserves the trailing letter."""
    s = PIIScrubber()
    text = "id 11010119900307887X here"
    result = s.scrub(text)
    recs = [r for r in result.redactions if r.category == "id_card"]
    assert len(recs) == 1
    masked = result.text.split("id ")[1].split(" here")[0]
    # Should preserve trailing X.
    assert masked.endswith("X") or masked.endswith("x")
    # And first 2 digits.
    assert masked.startswith("11")


def test_id_card_all_digit_uses_mask_keep_ends() -> None:
    """id_card without trailing X falls through to _mask_keep_ends."""
    s = PIIScrubber()
    # Real 18-digit (no X).
    text = "id 110101199003078871 here"
    result = s.scrub(text)
    recs = [r for r in result.redactions if r.category == "id_card"]
    assert len(recs) == 1
    masked = result.text.split("id ")[1].split(" here")[0]
    assert masked.startswith("11")  # first 2 preserved
    assert masked.endswith("71")  # last 2 preserved


def test_custom_categories_subset_only_applies_named() -> None:
    """scrub(text, categories=['email']) only masks email, leaves phone etc."""
    s = PIIScrubber()
    text = "mail me x@y.com or call 13812345678"
    result = s.scrub(text, categories=["email"])
    # Email masked; phone NOT masked (not in subset).
    assert "13812345678" in result.text
    assert "x@y.com" not in result.text


def test_all_documented_categories_present() -> None:
    """PII_CATEGORIES exports the 5 documented categories."""
    assert set(PII_CATEGORIES) == {"phone", "id_card", "bank_card", "email", "ipv4"}


def test_scrub_simple_returns_only_text() -> None:
    """scrub_simple returns just the text (no metadata)."""
    s = PIIScrubber()
    out = s.scrub_simple("call 13812345678")
    assert isinstance(out, str)
    assert "13812345678" not in out
