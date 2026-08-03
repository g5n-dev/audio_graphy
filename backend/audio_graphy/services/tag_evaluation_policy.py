"""Shared statistical policy for sealed tag evaluation and release preflight."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

WILSON_95_Z = 1.959963984540054
CRITICAL_RECALL_LCB_THRESHOLD = 0.95


def wilson_lower_bound(
    successes: int,
    total: int,
    *,
    z: float = WILSON_95_Z,
) -> float:
    """Return the two-sided Wilson score lower bound for a binomial rate."""

    if total <= 0:
        return 0.0
    probability = successes / total
    z_squared = z * z
    denominator = 1 + z_squared / total
    centre = probability + z_squared / (2 * total)
    margin = z * math.sqrt((probability * (1 - probability) + z_squared / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def minimum_perfect_wilson_support(
    *,
    threshold: float = CRITICAL_RECALL_LCB_THRESHOLD,
    z: float = WILSON_95_Z,
) -> int:
    """Return the least all-success support whose Wilson LCB reaches ``threshold``."""

    if not 0 < threshold < 1:
        raise ValueError("Wilson lower-bound threshold must be between zero and one")
    minimum = max(1, math.ceil((threshold * z * z) / (1 - threshold)))
    while wilson_lower_bound(minimum, minimum, z=z) < threshold:
        minimum += 1
    while minimum > 1 and wilson_lower_bound(minimum - 1, minimum - 1, z=z) >= threshold:
        minimum -= 1
    return minimum


def critical_enum_values(
    definition: Mapping[str, Any],
    *,
    observed_values: Sequence[str] = (),
) -> tuple[str, ...]:
    """Resolve the exact enum values governed by the critical-recall gate."""

    negative_values = {
        str(value) for value in definition.get("negative_values", []) if value is not None
    }
    critical_values: set[str] = set()
    configured = definition.get("critical_values")
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        critical_values.update(
            str(value)
            for value in configured
            if value is not None and str(value) not in negative_values
        )
    if definition.get("critical"):
        allowed = definition.get("allowed_values")
        registered = (
            [str(value) for value in allowed if value is not None]
            if isinstance(allowed, Sequence) and not isinstance(allowed, (str, bytes))
            else list(observed_values)
        )
        critical_values.update(value for value in registered if value not in negative_values)
    return tuple(sorted(critical_values))


__all__ = [
    "CRITICAL_RECALL_LCB_THRESHOLD",
    "WILSON_95_Z",
    "critical_enum_values",
    "minimum_perfect_wilson_support",
    "wilson_lower_bound",
]
