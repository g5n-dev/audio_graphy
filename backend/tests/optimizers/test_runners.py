"""Demo selection policy: the part of bootstrapping that has consequences.

Two failures matter more than the rest. A demo drawn from outside the train split
leaks the very rows the compiled candidate is later scored on, so its evaluation
reports a number it did not earn. And a demo the baseline gets *wrong* teaches the
model an answer no run ever produced -- a far weaker claim than "here is a case you
already handle".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from audio_graphy.optimizers.runners import (
    DemoBootstrapError,
    GoldExample,
    bootstrap_demo_candidates,
    eligible_for_bootstrap,
)


def _example(subject_id: int, *, split: str = "train", tag: str = "intent") -> GoldExample:
    return GoldExample(
        subject_type="dialogue_unit",
        subject_id=subject_id,
        split=split,
        rendered_text=f"对话 {subject_id}",
        truths=({"tag_key": tag, "value": "purchase"},),
        gold_label_id=subject_id * 10,
        reception_id=7,
        segment_ids=(subject_id,),
    )


class StubRunner:
    """Returns a canned prediction per subject, and records what it was asked."""

    def __init__(self, *, by_subject: Mapping[int, Any] | None = None) -> None:
        self.by_subject = dict(by_subject or {})
        self.seen: list[int] = []

    async def predict(
        self,
        *,
        subject_type: str,
        subject_id: int,
        input_snapshot: Mapping[str, Any],
        harness_spec: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.seen.append(subject_id)
        outcome = self.by_subject.get(subject_id, {"assignments": [{"tag_key": "intent"}]})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubScorer:
    """Scores by subject id so each test can state the outcome it means to test."""

    def __init__(self, scores: Mapping[str, float], *, default: float = 0.0) -> None:
        self.scores = dict(scores)
        self.default = default

    def score(
        self,
        *,
        truths: Sequence[Mapping[str, Any]],
        assignments: Mapping[str, Any],
    ) -> float:
        return float(self.scores.get(str(assignments.get("marker")), self.default))


def _snapshots(*subject_ids: int) -> dict[tuple[str, int], dict[str, Any]]:
    return {("dialogue_unit", sid): {"text": f"对话 {sid}"} for sid in subject_ids}


# ------------------------------------------------------------------- eligibility


def test_only_the_train_split_can_supply_demos() -> None:
    """A dev or test row inlined as a demo makes the later evaluation meaningless."""

    eligible = eligible_for_bootstrap(
        [
            _example(1, split="train"),
            _example(2, split="dev"),
            _example(3, split="test"),
            _example(4, split="train"),
        ]
    )

    assert [item.subject_id for item in eligible] == [1, 4]


def test_eligibility_is_ordered_so_two_identical_compiles_agree() -> None:
    eligible = eligible_for_bootstrap([_example(9), _example(2), _example(5)])

    assert [item.subject_id for item in eligible] == [2, 5, 9]


def test_the_bootstrap_cap_bounds_how_much_inference_a_compile_buys() -> None:
    eligible = eligible_for_bootstrap([_example(i) for i in range(1, 20)], limit=3)

    assert [item.subject_id for item in eligible] == [1, 2, 3]


def test_a_non_positive_cap_is_refused() -> None:
    with pytest.raises(DemoBootstrapError, match="positive"):
        eligible_for_bootstrap([_example(1)], limit=0)


# -------------------------------------------------------------------- bootstrap


@pytest.mark.asyncio
async def test_only_examples_the_baseline_already_gets_right_become_demos() -> None:
    runner = StubRunner(
        by_subject={
            1: {"marker": "perfect"},
            2: {"marker": "partial"},
        }
    )
    scorer = StubScorer({"perfect": 1.0, "partial": 0.5})

    selected = await bootstrap_demo_candidates(
        [_example(1), _example(2)],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(1, 2),
        harness_spec={},
    )

    assert [item.example.subject_id for item in selected] == [1]


@pytest.mark.asyncio
async def test_a_prediction_error_skips_the_example_rather_than_scoring_it_zero() -> None:
    """Scoring a crash as zero would bias selection toward short, easy conversations."""

    runner = StubRunner(by_subject={1: RuntimeError("provider timeout"), 2: {"marker": "ok"}})
    scorer = StubScorer({"ok": 1.0})

    selected = await bootstrap_demo_candidates(
        [_example(1), _example(2)],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(1, 2),
        harness_spec={},
    )

    assert [item.example.subject_id for item in selected] == [2]
    assert runner.seen == [1, 2], "the failing subject must still have been attempted"


@pytest.mark.asyncio
async def test_an_example_with_no_input_snapshot_is_skipped_without_calling_the_model() -> None:
    runner = StubRunner()
    scorer = StubScorer({}, default=1.0)

    selected = await bootstrap_demo_candidates(
        [_example(1), _example(2)],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(2),
        harness_spec={},
    )

    assert runner.seen == [2]
    assert [item.example.subject_id for item in selected] == [2]


@pytest.mark.asyncio
async def test_results_are_ordered_by_score_then_subject() -> None:
    runner = StubRunner(
        by_subject={1: {"marker": "good"}, 2: {"marker": "best"}, 3: {"marker": "good"}}
    )
    scorer = StubScorer({"good": 1.0, "best": 1.0})

    selected = await bootstrap_demo_candidates(
        [_example(3), _example(1), _example(2)],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(1, 2, 3),
        harness_spec={},
        score_floor=1.0,
    )

    assert [item.example.subject_id for item in selected] == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_lowered_floor_admits_imperfect_examples() -> None:
    runner = StubRunner(by_subject={1: {"marker": "partial"}})
    scorer = StubScorer({"partial": 0.8})

    selected = await bootstrap_demo_candidates(
        [_example(1)],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(1),
        harness_spec={},
        score_floor=0.75,
    )

    assert [item.score for item in selected] == [0.8]


@pytest.mark.asyncio
async def test_the_dev_split_never_reaches_the_model_at_all() -> None:
    """Not merely filtered from the result -- never predicted, so never billed."""

    runner = StubRunner()
    scorer = StubScorer({}, default=1.0)

    await bootstrap_demo_candidates(
        [_example(1, split="dev"), _example(2, split="train")],
        runner=runner,
        scorer=scorer,
        input_snapshots=_snapshots(1, 2),
        harness_spec={},
    )

    assert runner.seen == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("floor", [-0.1, 1.1])
async def test_a_floor_outside_the_unit_interval_is_refused(floor: float) -> None:
    with pytest.raises(DemoBootstrapError, match=r"\[0, 1\]"):
        await bootstrap_demo_candidates(
            [_example(1)],
            runner=StubRunner(),
            scorer=StubScorer({}),
            input_snapshots=_snapshots(1),
            harness_spec={},
            score_floor=floor,
        )


@pytest.mark.asyncio
async def test_the_selected_assignments_are_copied_not_aliased() -> None:
    # The caller renders these into a prompt; a live reference to the runner's
    # mapping would let a later prediction rewrite an already-chosen demo.
    prediction: dict[str, Any] = {"marker": "ok", "assignments": [{"tag_key": "intent"}]}
    runner = StubRunner(by_subject={1: prediction})

    selected = await bootstrap_demo_candidates(
        [_example(1)],
        runner=runner,
        scorer=StubScorer({"ok": 1.0}),
        input_snapshots=_snapshots(1),
        harness_spec={},
    )

    prediction["marker"] = "mutated"
    assert selected[0].assignments["marker"] == "ok"
