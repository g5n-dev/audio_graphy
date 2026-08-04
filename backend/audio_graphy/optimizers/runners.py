"""Dependency inversion for the two service capabilities a compile needs.

``optimizers`` sits below ``services`` in the layering, so it cannot reach
``TagExtractor`` to run a prediction nor ``compute_evaluation_summary`` to score one.
Both are declared here as protocols and bound by ``optimizer_worker``, which is free
to import either side.

The policy that decides *which* predictions become few-shot demos lives here rather
than in the worker, because it is the part with real consequences: a demo that the
baseline already gets wrong teaches the wrong answer, and a demo drawn from outside
the training split leaks the set the candidate is later measured on.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: A demo must be one the baseline already handles well. Below this the example is
#: teaching an answer the tagger did not actually produce, which is a different and
#: much weaker claim than "here is a case you got right".
DEFAULT_DEMO_SCORE_FLOOR = 0.999

#: Bootstrapping runs real inference over real conversations. The cap is what keeps
#: a compile's provider spend proportional to the handful of demos it can inline.
DEFAULT_MAX_BOOTSTRAP_SUBJECTS = 64


class DemoBootstrapError(RuntimeError):
    """Raised when demo selection cannot proceed on the data it was given."""


@dataclass(frozen=True, slots=True)
class GoldExample:
    """One reviewed subject, its truth rows, and the text a demo would quote."""

    subject_type: str
    subject_id: int
    split: str
    rendered_text: str
    truths: tuple[Mapping[str, Any], ...]
    gold_label_id: int
    reception_id: int | None = None
    segment_ids: tuple[int, ...] = ()
    recording_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoredExample:
    """A gold example with the score the baseline earned on it."""

    example: GoldExample
    score: float
    assignments: Mapping[str, Any] = field(default_factory=dict)


class PredictionRunner(Protocol):
    """Run one subject through the tagger and return its assignments.

    Bound by the worker to a real ``TagExtractor`` call. Implementations must not
    publish: bootstrapping is a measurement, and writing its output would let a
    compile change the tags users see.
    """

    async def predict(
        self,
        *,
        subject_type: str,
        subject_id: int,
        input_snapshot: Mapping[str, Any],
        harness_spec: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ExampleScorer(Protocol):
    """Score one set of assignments against the reviewed truth for that subject."""

    def score(
        self,
        *,
        truths: Sequence[Mapping[str, Any]],
        assignments: Mapping[str, Any],
    ) -> float: ...


def eligible_for_bootstrap(
    examples: Sequence[GoldExample],
    *,
    limit: int = DEFAULT_MAX_BOOTSTRAP_SUBJECTS,
) -> tuple[GoldExample, ...]:
    """Narrow *examples* to the train split, deterministically, up to *limit*.

    Restricting to ``train`` is the whole guard against leakage: dev and test rows
    are what the compiled candidate is later evaluated on, and an inlined demo drawn
    from them would make the evaluation report a number the candidate did not earn.
    """

    if limit < 1:
        raise DemoBootstrapError("bootstrap limit must be positive")
    train = [example for example in examples if example.split == "train"]
    train.sort(key=lambda example: (example.subject_type, example.subject_id))
    return tuple(train[:limit])


async def bootstrap_demo_candidates(
    examples: Sequence[GoldExample],
    *,
    runner: PredictionRunner,
    scorer: ExampleScorer,
    input_snapshots: Mapping[tuple[str, int], Mapping[str, Any]],
    harness_spec: Mapping[str, Any],
    score_floor: float = DEFAULT_DEMO_SCORE_FLOOR,
    limit: int = DEFAULT_MAX_BOOTSTRAP_SUBJECTS,
) -> tuple[ScoredExample, ...]:
    """Replay the baseline over train-split examples and keep the ones it gets right.

    A subject whose prediction fails outright is skipped, not scored zero: an
    inference error says nothing about whether the example would make a good demo,
    and recording it as a zero would quietly bias selection toward short, easy
    conversations.

    Results come back sorted by score, then by subject, so an identical compile
    request produces an identical demo set.
    """

    if not 0.0 <= score_floor <= 1.0:
        raise DemoBootstrapError("demo score floor must be within [0, 1]")

    scored: list[ScoredExample] = []
    for example in eligible_for_bootstrap(examples, limit=limit):
        snapshot = input_snapshots.get((example.subject_type, example.subject_id))
        if snapshot is None:
            continue
        try:
            assignments = await runner.predict(
                subject_type=example.subject_type,
                subject_id=example.subject_id,
                input_snapshot=snapshot,
                harness_spec=harness_spec,
            )
        except Exception:
            # Logged, not counted: without this line a compile where every
            # prediction crashed is indistinguishable from one where the baseline
            # simply got nothing right.
            logger.warning(
                "demo bootstrap prediction failed subject=%s:%s",
                example.subject_type,
                example.subject_id,
                exc_info=True,
            )
            continue
        score = scorer.score(truths=example.truths, assignments=assignments)
        if score >= score_floor:
            scored.append(
                ScoredExample(example=example, score=score, assignments=dict(assignments))
            )

    scored.sort(
        key=lambda item: (
            -item.score,
            item.example.subject_type,
            item.example.subject_id,
        )
    )
    return tuple(scored)
