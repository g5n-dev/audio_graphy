"""Canonical reception-audio geometry invariants."""

from __future__ import annotations

import random

import pytest

from audio_graphy.core.audio_timeline import (
    AudioTimelinePlanner,
    AudioTimelineSource,
    seconds_to_milliseconds,
)


def test_planner_derives_dense_integer_timeline_with_explicit_silence() -> None:
    plan = AudioTimelinePlanner().plan(
        [
            AudioTimelineSource(
                source_id="first",
                source_start_ms=0,
                source_end_ms=1_250,
                verified_duration_ms=4_000,
            ),
            AudioTimelineSource(
                source_id="second",
                source_start_ms=500,
                source_end_ms=2_000,
                verified_duration_ms=3_000,
                gap_before_ms=750,
            ),
        ]
    )

    assert plan.total_duration_ms == 3_500
    assert [
        (
            item.sequence_no,
            item.timeline_start_ms,
            item.timeline_end_ms,
            item.source_start_ms,
            item.source_end_ms,
            item.gap_before_ms,
        )
        for item in plan.slices
    ] == [
        (0, 0, 1_250, 0, 1_250, 0),
        (1, 2_000, 3_500, 500, 2_000, 750),
    ]


@pytest.mark.parametrize(
    "source_kwargs",
    [
        [
            {
                "source_id": "first",
                "source_start_ms": 0,
                "source_end_ms": 1_000,
                "verified_duration_ms": 1_000,
                "gap_before_ms": 1,
            }
        ],
        [
            {
                "source_id": "first",
                "source_start_ms": 0,
                "source_end_ms": 1_001,
                "verified_duration_ms": 1_000,
            }
        ],
        [
            {
                "source_id": "first",
                "source_start_ms": 500,
                "source_end_ms": 500,
                "verified_duration_ms": 1_000,
            }
        ],
        [
            {
                "source_id": "duplicate",
                "source_start_ms": 0,
                "source_end_ms": 500,
                "verified_duration_ms": 1_000,
            },
            {
                "source_id": "duplicate",
                "source_start_ms": 500,
                "source_end_ms": 1_000,
                "verified_duration_ms": 1_000,
            },
        ],
    ],
)
def test_planner_rejects_invalid_or_ambiguous_geometry(
    source_kwargs: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        sources = [AudioTimelineSource(**kwargs) for kwargs in source_kwargs]  # type: ignore[arg-type]
        AudioTimelinePlanner().plan(sources)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, 0),
        (0.0005, 1),
        (1.2344, 1_234),
        (1.2345, 1_235),
    ],
)
def test_seconds_conversion_is_deterministic(seconds: float, expected: int) -> None:
    assert seconds_to_milliseconds(seconds) == expected


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf"), True])
def test_seconds_conversion_rejects_invalid_values(seconds: float) -> None:
    with pytest.raises(ValueError):
        seconds_to_milliseconds(seconds)


def test_randomized_timeline_preserves_every_source_span_and_gap() -> None:
    """Model-check the planner on a deterministic corpus of valid timelines."""

    rng = random.Random(0xA6D10)
    planner = AudioTimelinePlanner()

    for case_no in range(500):
        sources: list[AudioTimelineSource] = []
        for source_no in range(rng.randint(1, 24)):
            verified_duration_ms = rng.randint(1, 3_600_000)
            source_start_ms = rng.randint(0, verified_duration_ms - 1)
            source_end_ms = rng.randint(source_start_ms + 1, verified_duration_ms)
            sources.append(
                AudioTimelineSource(
                    source_id=f"{case_no}:{source_no}",
                    source_start_ms=source_start_ms,
                    source_end_ms=source_end_ms,
                    verified_duration_ms=verified_duration_ms,
                    gap_before_ms=0 if source_no == 0 else rng.randint(0, 120_000),
                )
            )

        plan = planner.plan(sources)
        cursor_ms = 0
        for sequence_no, (source, planned) in enumerate(zip(sources, plan.slices, strict=True)):
            assert planned.sequence_no == sequence_no
            assert planned.source_start_ms == source.source_start_ms
            assert planned.source_end_ms == source.source_end_ms
            assert planned.timeline_start_ms == cursor_ms + source.gap_before_ms
            assert (
                planned.timeline_end_ms - planned.timeline_start_ms
                == source.source_end_ms - source.source_start_ms
            )
            cursor_ms = planned.timeline_end_ms

        assert plan.total_duration_ms == cursor_ms
