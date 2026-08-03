"""Canonical integer geometry for reception audio.

Seconds remain part of the legacy HTTP/database contract, but all validation
and cursor arithmetic in this module uses integer milliseconds.  This avoids
drift between source clips, logical playback, and physical assembly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def seconds_to_milliseconds(value: int | float) -> int:
    """Convert non-negative seconds with deterministic half-up rounding."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a finite non-negative number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("seconds must be a finite non-negative number")
    try:
        return int(
            (Decimal(str(parsed)) * Decimal(1_000)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("seconds must be a finite non-negative number") from exc


def milliseconds_to_seconds(value: int) -> float:
    """Project a validated integer millisecond value to legacy seconds."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("milliseconds must be a non-negative integer")
    return value / 1_000


def verified_recording_duration_ms(recording: object) -> int | None:
    """Read the additive Recording duration contract without coupling migrations.

    ``audio_duration_ms`` is the canonical field.  The two aliases keep this
    helper usable while an expand/backfill migration is rolling through old
    application versions.
    """
    for field_name, multiplier in (
        ("audio_duration_ms", 1),
        ("duration_ms", 1),
        ("audio_duration_sec", 1_000),
    ):
        raw = getattr(recording, field_name, None)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        parsed = float(raw)
        if not math.isfinite(parsed) or parsed <= 0:
            continue
        if multiplier == 1:
            return int(parsed)
        return seconds_to_milliseconds(parsed)
    return None


@dataclass(frozen=True, slots=True)
class AudioTimelineSource:
    """One verified immutable source interval requested for a timeline."""

    source_id: str | int
    source_start_ms: int
    source_end_ms: int
    verified_duration_ms: int
    gap_before_ms: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.source_id, str) and not self.source_id:
            raise ValueError("source_id must not be empty")
        if isinstance(self.source_id, bool) or not isinstance(self.source_id, (str, int)):
            raise ValueError("source_id must be a string or integer")
        for field_name in (
            "source_start_ms",
            "source_end_ms",
            "verified_duration_ms",
            "gap_before_ms",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field_name} must be an integer")
        if self.source_start_ms < 0 or self.gap_before_ms < 0:
            raise ValueError("source start and gap must be non-negative")
        if self.verified_duration_ms <= 0:
            raise ValueError("verified duration must be positive")
        if not self.source_start_ms < self.source_end_ms <= self.verified_duration_ms:
            raise ValueError("source interval must fit within verified duration")


@dataclass(frozen=True, slots=True)
class PlannedAudioSlice:
    """One source interval positioned on the canonical timeline."""

    source_id: str | int
    sequence_no: int
    source_start_ms: int
    source_end_ms: int
    gap_before_ms: int
    timeline_start_ms: int
    timeline_end_ms: int

    @property
    def source_duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms


@dataclass(frozen=True, slots=True)
class AudioTimelinePlan:
    """Validated immutable timeline."""

    slices: tuple[PlannedAudioSlice, ...]
    total_duration_ms: int


class AudioTimelinePlanner:
    """Validate source geometry and derive the only legal reception timeline."""

    def plan(self, sources: Sequence[AudioTimelineSource]) -> AudioTimelinePlan:
        if isinstance(sources, (str, bytes)) or not sources:
            raise ValueError("at least one audio source is required")
        if sources[0].gap_before_ms != 0:
            raise ValueError("the first source cannot have a preceding gap")
        source_ids = [source.source_id for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")

        cursor_ms = 0
        planned: list[PlannedAudioSlice] = []
        for sequence_no, source in enumerate(sources):
            timeline_start_ms = cursor_ms + source.gap_before_ms
            timeline_end_ms = timeline_start_ms + (source.source_end_ms - source.source_start_ms)
            planned.append(
                PlannedAudioSlice(
                    source_id=source.source_id,
                    sequence_no=sequence_no,
                    source_start_ms=source.source_start_ms,
                    source_end_ms=source.source_end_ms,
                    gap_before_ms=source.gap_before_ms,
                    timeline_start_ms=timeline_start_ms,
                    timeline_end_ms=timeline_end_ms,
                )
            )
            cursor_ms = timeline_end_ms
        return AudioTimelinePlan(
            slices=tuple(planned),
            total_duration_ms=cursor_ms,
        )


__all__ = [
    "AudioTimelinePlan",
    "AudioTimelinePlanner",
    "AudioTimelineSource",
    "PlannedAudioSlice",
    "milliseconds_to_seconds",
    "seconds_to_milliseconds",
    "verified_recording_duration_ms",
]
