"""Deterministic language bucketing for query-semantic cache isolation."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

SemanticLanguage = Literal["zh-CN", "en", "und"]

_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2FA1F),  # Supplementary CJK extensions/compatibility
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9._%+-])"
)
_UPPER_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9._:/-])[A-Z][A-Z0-9._:/-]{1,63}(?![A-Za-z0-9._:/-])"
)
_LABELED_IDENTIFIER_RE = re.compile(
    r"(?:\b(?:id|sku|code|ticket)\b|\border\s+(?:id|number|no)\b|订单号|单号|编号|编码)"
    r"\s*[:：#=-]?\s*([A-Za-z][A-Za-z0-9._:/-]{1,63})",
    re.IGNORECASE,
)


def _is_cjk_ideograph(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _CJK_RANGES)


def detect_semantic_language(text: str) -> SemanticLanguage:
    """Bucket text without an LLM so semantic reuse never crosses languages.

    Any CJK ideograph selects ``zh-CN``. Otherwise text whose language-bearing
    letters are all Latin selects ``en``; digits, punctuation, other scripts,
    and empty input select ``und``.
    """

    if any(_is_cjk_ideograph(character) for character in text):
        return "zh-CN"
    letters = [character for character in text if character.isalpha()]
    if letters and all(
        unicodedata.name(character, "").startswith("LATIN ") for character in letters
    ):
        return "en"
    return "und"


def semantic_protected_identifiers(text: str) -> tuple[str, ...]:
    """Extract identifiers that semantic cache reuse must match exactly.

    Numbers, dates, and mixed alpha-numeric identifiers are also derived
    defensively inside the cache coordinator. This helper adds identifiers
    that cannot be distinguished from ordinary words without query context:
    uppercase business tokens, e-mail addresses, and values following common
    ID/SKU/order labels.
    """

    normalized = unicodedata.normalize("NFC", text)
    values = set(_EMAIL_RE.findall(normalized))
    values.update(_UPPER_IDENTIFIER_RE.findall(normalized))
    values.update(_LABELED_IDENTIFIER_RE.findall(normalized))
    return tuple(sorted(value for value in values if value))


__all__ = [
    "SemanticLanguage",
    "detect_semantic_language",
    "semantic_protected_identifiers",
]
