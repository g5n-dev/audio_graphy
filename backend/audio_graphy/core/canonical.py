"""Content-addressing helpers shared by the tag pipeline and the prompt compiler.

Both functions are pure and dependency-free. They live here rather than in the
service that first needed them because more than one layer now content-addresses the
same values: a checksum computed by the compiler has to equal the one recorded in a
search manifest, and a token estimate made at compile time has to equal the one the
extractor enforces at serve time. Two implementations of either would be a defect
waiting to happen, and importing them upward from ``services`` made ``optimizers``
and ``services`` mutually dependent.

``services.tag_governance`` and ``services.tag_harness_runtime`` re-export these under
their original names, so existing importers are unaffected.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def json_normalize(value: Any) -> Any:
    """Normalize a value so equivalent payloads serialize identically.

    Dict keys are sorted and stringified; floats that are whole numbers collapse to
    ints, and the rest are rounded, so a value that survives a JSON round-trip
    checksums the same as the value that produced it.
    """

    if isinstance(value, dict):
        return {str(key): json_normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [json_normalize(item) for item in value]
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 9)
    return value


def canonical_checksum(value: Any) -> str:
    """SHA-256 of normalized JSON, used for immutable version snapshots."""

    payload = json.dumps(
        json_normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_prompt_tokens(value: str) -> int:
    """Conservative, tokenizer-independent proxy suitable for a hard preflight.

    Raises:
        TypeError: if ``value`` is not a string. Callers that owe their own error
            type -- the Harness spec validator, for one -- catch and translate it.
    """

    if not isinstance(value, str):
        raise TypeError("prompt content must be a string")
    if not value:
        return 0
    ascii_count = sum(character.isascii() for character in value)
    non_ascii_count = len(value) - ascii_count
    character_proxy = non_ascii_count + ((ascii_count + 3) // 4)
    byte_proxy = (len(value.encode("utf-8")) + 3) // 4
    return max(character_proxy, byte_proxy)
