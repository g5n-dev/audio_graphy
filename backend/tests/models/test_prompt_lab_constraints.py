"""Constraints that carry policy, not just data integrity.

The silver-label fences and the terminal-immutability trigger are the two places
where a database rule replaces a promise. Both are easy to weaken by accident -- one
by relaxing a CHECK, the other by adding a column to ``tagger_versions`` and
forgetting that the trigger enumerates columns by hand -- so both are asserted here
rather than left to review.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

import audio_graphy.models  # noqa: F401
from audio_graphy.models.base import Base

_REJECTED = (IntegrityError, OperationalError, DatabaseError)

_SILVER_INSERT = text(
    """
    INSERT INTO tag_silver_labels
        (tenant_id, created_at, updated_at, subject_type, subject_id, tag_key,
         evidence_refs, truth_state, truth_tier, split, teacher_model_tier,
         agreement_count, source)
    VALUES
        (:tenant_id, NOW(), NOW(), 'dialogue_unit', :subject_id, 'intent',
         '[]', 'present', :truth_tier, :split, 'strong', 1, 'strong_critic')
    """
)


def _insert_silver(session: Session, **overrides: Any) -> None:
    params: dict[str, Any] = {
        "tenant_id": "constraint-test",
        "subject_id": 1,
        "truth_tier": "t1",
        "split": "train",
    }
    params.update(overrides)
    session.execute(_SILVER_INSERT, params)
    session.commit()


def test_a_silver_label_cannot_reach_an_evaluation_lane(db_session: Session) -> None:
    """Machine labels are barred from validation/holdout by the database itself."""

    for split in ("validation", "challenge", "holdout", "audit"):
        with pytest.raises(_REJECTED):
            _insert_silver(db_session, subject_id=10, split=split)
        db_session.rollback()


def test_a_silver_label_cannot_reach_the_tier_a_gold_freeze_requires(
    db_session: Session,
) -> None:
    """Freezing a gold set requires t2/t3; silver labels are capped below that."""

    for tier in ("t2", "t3"):
        with pytest.raises(_REJECTED):
            _insert_silver(db_session, subject_id=11, truth_tier=tier)
        db_session.rollback()


def test_a_train_lane_silver_label_is_accepted(db_session: Session) -> None:
    """The control: the fences reject the disallowed cases, not everything."""

    _insert_silver(db_session, subject_id=12)

    count = db_session.execute(
        text("SELECT COUNT(*) FROM tag_silver_labels WHERE tenant_id = 'constraint-test'")
    ).scalar()
    assert count == 1


def _migration_immutable_columns() -> set[str]:
    """Read _IMMUTABLE_COLUMNS out of the migration without importing it.

    ``backend/alembic/`` is a package on the path and shadows the installed ``alembic``
    distribution, so importing the revision module here would fail on ``from alembic
    import op``. Parsing the literal sidesteps that and keeps the assertion honest:
    it reads the value the migration actually ships.
    """

    source = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0033_prompt_lab.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_IMMUTABLE_COLUMNS"
            for target in node.targets
        ):
            return {str(value) for value in ast.literal_eval(node.value)}
    raise AssertionError("0033_prompt_lab no longer defines _IMMUTABLE_COLUMNS")


def test_terminal_immutability_trigger_covers_every_tagger_version_column() -> None:
    """A column the trigger does not name is a column a qualified version can change.

    The trigger compares ``NEW.x <=> OLD.x`` column by column, from the list this
    migration ships. Adding a column to ``tagger_versions`` without extending that
    list silently reopens terminal rows for mutation through the new column -- so the
    coverage is asserted here rather than left to whoever adds the next column.
    """

    # status and qualified_at define the one permitted transition, so they are
    # compared differently; updated_at is expected to move when it happens.
    exempt = {"status", "qualified_at", "updated_at"}
    model_columns = set(Base.metadata.tables["tagger_versions"].columns.keys()) - exempt

    frozen = _migration_immutable_columns()

    assert not (model_columns - frozen), (
        "these tagger_versions columns are not frozen by the terminal trigger, so a "
        f"qualified version can be rewritten through them: {sorted(model_columns - frozen)}"
    )
    assert not (frozen - model_columns), (
        f"the trigger freezes columns that no longer exist: {sorted(frozen - model_columns)}"
    )
