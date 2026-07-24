"""PIIScrubber — regex-based PII redactor.

PIPL §14.3 implementation: covers 6 categories of PII commonly found in
Chinese store / consultative recordings::

    phone      — 11-digit Chinese mobile (1[3-9]\\d{9}) and landline (0\\d{2,3}-?\\d{7,8})
    id_card    — 18-digit Chinese identity card (\\d{17}[\\dXx])
    bank_card  — 16-19 digit continuous number
    email      — standard RFC-ish
    ipv4       — dotted-quad, octets 0-255

Order of application: ``id_card`` is matched before ``bank_card`` (id_card
is a strict subset of 16-19 digit numbers and is more specific). To prevent
re-scanning inside an already-redacted span, replacement is done in a single
left-to-right pass with non-overlapping spans.

Replacement format (per locked decision):
    - phone (mobile): ``138****1234`` (keep first 3 + last 4; 4 stars)
    - phone (landline): ``010****5678`` (same rule applied to digits)
    - id_card:         ``11**********34`` (keep first 2 + last 2)
    - bank_card:       ``62***************8`` (keep first 2 + last 1)
    - email:           ``ab***@example.com`` (first 2 + 3 stars + @domain)
    - ipv4:            ``10.0.**.**`` (mask last 2 octets)

Idempotence: scrubbing an already-scrubbed text returns the same text
(no PII left → no matches). This is verified by tests/test_pii.py.

Chinese name recognition is out of scope for M6 (PRD §4.3); M7+ will
add a surname-dict + HanLP-based NER layer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

PII_CATEGORIES: tuple[str, ...] = (
    "phone",
    "id_card",
    "bank_card",
    "email",
    "ipv4",
)

_CATEGORY_PRIORITY: dict[str, int] = {
    "id_card": 0,  # most specific (18-digit strict) — wins over bank_card
    "phone": 1,
    "bank_card": 2,
    "email": 3,
    "ipv4": 4,
}


@dataclass(frozen=True, slots=True)
class RedactionRecord:
    """One PII detection hit (no original text — minimises leakage).

    Attributes:
        category: One of PII_CATEGORIES.
        start: Start offset in the ORIGINAL text (pre-redaction).
        end: End offset (exclusive) in the ORIGINAL text.
        original_length: Length of the matched substring.
    """

    category: str
    start: int
    end: int
    original_length: int


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """Output of PIIScrubber.scrub.

    Attributes:
        text: Scrubbed text with PII replaced by masks.
        redactions: List of RedactionRecord entries (ordered by start).
    """

    text: str
    redactions: list[RedactionRecord] = field(default_factory=list)


class PIIScrubber:
    """Regex-based PII redactor. 中文姓名识别推迟 M7+.

    Args:
        redaction_char: Character used for masking. Default ``"*"``.
    """

    CATEGORIES: ClassVar[tuple[str, ...]] = PII_CATEGORIES

    _PATTERNS: ClassVar[dict[str, re.Pattern[str]]] = {
        # Chinese mobile: 1[3-9] + 9 more digits, no surrounding digits.
        "phone": re.compile(
            r"(?<!\d)(1[3-9]\d{9})(?!\d)"
            r"|"
            # Chinese landline: area code 0XX/0XX- + 7-8 digits, optional hyphen.
            r"(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)"
        ),
        # 18-digit Chinese ID (last char may be X/x).
        "id_card": re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)"),
        # 16-19 digit bank card; first char non-zero.
        "bank_card": re.compile(r"(?<!\d)([1-9]\d{15,18})(?!\d)"),
        # Email — simplified RFC.
        "email": re.compile(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"),
        # IPv4 — 4 octets 0-255.
        "ipv4": re.compile(
            r"(?<!\d)((?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
            r"(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){3})(?!\d)"
        ),
    }

    def __init__(self, *, redaction_char: str = "*") -> None:
        if len(redaction_char) != 1:
            raise ValueError("redaction_char must be a single character")
        self._redaction_char = redaction_char

    def scrub(
        self,
        text: str,
        *,
        categories: Sequence[str] = PII_CATEGORIES,
    ) -> ScrubResult:
        """Apply all PII rules left-to-right. Idempotent.

        Args:
            text: Input text.
            categories: Optional subset of PII_CATEGORIES.

        Returns:
            ScrubResult with redacted text + per-hit records.
        """
        if not text:
            return ScrubResult(text=text, redactions=[])

        # Resolve + order categories by priority (id_card before bank_card, etc.).
        enabled = [c for c in categories if c in self._PATTERNS]
        # We always sort by priority so overlapping categories prefer specific ones.
        enabled.sort(key=lambda c: _CATEGORY_PRIORITY.get(c, 99))

        # Collect (start, end, category) candidates.
        candidates: list[tuple[int, int, str]] = []
        for cat in enabled:
            for m in self._PATTERNS[cat].finditer(text):
                # For combined patterns (phone has two alternations) the
                # matched group is whichever branch fired.
                start, end = m.start(0), m.end(0)
                if start == end:
                    continue
                candidates.append((start, end, cat))

        # Sort by start asc, then longer span first, then category priority.
        candidates.sort(key=lambda t: (t[0], -(t[1] - t[0]), _CATEGORY_PRIORITY.get(t[2], 99)))

        # Greedy non-overlapping selection.
        chosen: list[tuple[int, int, str]] = []
        last_end = -1
        for start, end, cat in candidates:
            if start >= last_end:
                chosen.append((start, end, cat))
                last_end = end

        if not chosen:
            return ScrubResult(text=text, redactions=[])

        # Build output by walking chosen spans in order.
        out_parts: list[str] = []
        cursor = 0
        records: list[RedactionRecord] = []
        for start, end, cat in chosen:
            out_parts.append(text[cursor:start])
            original = text[start:end]
            replacement = self._mask(cat, original)
            out_parts.append(replacement)
            records.append(
                RedactionRecord(
                    category=cat,
                    start=start,
                    end=end,
                    original_length=len(original),
                )
            )
            cursor = end
        out_parts.append(text[cursor:])

        return ScrubResult(text="".join(out_parts), redactions=records)

    def scrub_simple(self, text: str) -> str:
        """Convenience: return only the scrubbed text (no metadata)."""
        return self.scrub(text).text

    # ------------------------------------------------------------------
    # Masking helpers
    # ------------------------------------------------------------------
    def _mask(self, category: str, original: str) -> str:
        """Convert one matched span to its masked replacement."""
        ch = self._redaction_char

        if category == "email":
            at_idx = original.find("@")
            if at_idx <= 1:
                # Short local part — mask all but first char.
                local = original[0] if original else ""
                domain = original[at_idx:] if at_idx >= 0 else ""
                return f"{local}{ch * 3}{domain}"
            keep_local = original[: min(2, at_idx)]
            domain = original[at_idx:]
            return f"{keep_local}{ch * 3}{domain}"

        if category == "ipv4":
            octets = original.split(".")
            masked = octets[:]
            for i in range(max(0, len(masked) - 2), len(masked)):
                masked[i] = ch * 2
            return ".".join(masked)

        # Digit-based categories — extract digits so hyphens / spaces don't
        # distort the keep-first/last logic.
        digits = re.sub(r"\D", "", original)
        if not digits:
            return ch * max(len(original), 4)

        if category == "phone":
            if len(digits) == 11:
                # Mobile: 138****1234
                return f"{digits[:3]}{ch * 4}{digits[7:]}"
            # Landline: keep first 3 + last 4, mask middle.
            return self._mask_keep_ends(digits, keep_first=3, keep_last=4, mask_len=4)

        if category == "id_card":
            # Keep first 2 + last 2 (preserve trailing X).
            tail_char = original[-1] if not original[-1].isdigit() else None
            if tail_char is not None:
                # Preserve trailing X/x; mask digit middle.
                digit_part = digits
                head = digit_part[:2]
                tail = digit_part[-2:] + tail_char
                stars = ch * max(4, len(digit_part) - 4)
                return f"{head}{stars}{tail}"
            return self._mask_keep_ends(original, keep_first=2, keep_last=2, mask_len=10)

        if category == "bank_card":
            return self._mask_keep_ends(original, keep_first=2, keep_last=1, mask_len=15)

        # Fallback (shouldn't be reached).
        return ch * max(len(original), 4)

    def _mask_keep_ends(
        self,
        s: str,
        *,
        keep_first: int,
        keep_last: int,
        mask_len: int,
    ) -> str:
        """Keep first N + last M chars; replace middle with fixed stars."""
        if len(s) <= keep_first + keep_last:
            # Too short to keep + mask — mask everything.
            return self._redaction_char * max(len(s), mask_len)
        return s[:keep_first] + self._redaction_char * mask_len + s[len(s) - keep_last :]


_DEFAULT_PII_SCRUBBER = PIIScrubber()


def scrubbed_segment_text(
    text_scrubbed: str | None,
    transcript: str | None,
    *,
    scrubber: PIIScrubber | None = None,
) -> str:
    """Resolve segment text without ever exposing a legacy raw fallback.

    Persisted ``text_scrubbed`` remains authoritative, including an explicitly
    empty string. Legacy rows with a NULL scrubbed value are sanitized at the
    read boundary before they can enter responses or downstream analysis.
    """
    if text_scrubbed is not None:
        return text_scrubbed
    return (scrubber or _DEFAULT_PII_SCRUBBER).scrub_simple(transcript or "")


__all__ = [
    "PII_CATEGORIES",
    "PIIScrubber",
    "RedactionRecord",
    "ScrubResult",
    "scrubbed_segment_text",
]
