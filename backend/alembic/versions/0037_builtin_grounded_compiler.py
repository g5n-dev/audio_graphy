"""Admit ``builtin_grounded`` as a compiler an artifact may name.

The grounded proposer conditions a model on the current rule plus the reviewed
failures, exactly as DSPy's ``GroundedProposer`` does, but owns its meta-prompt and
needs no optional extra. It is a *different* compiler from ``builtin``, not a variant
of it: one spends provider budget and one does not.

Labelling it ``builtin`` and leaving ``compiler_version`` to tell them apart was the
cheaper option and is exactly the failure this table's CHECK exists to prevent -- an
artifact whose ``compiler`` field does not name what produced it makes every later
comparison between compilers read fabricated data.

MySQL cannot alter a CHECK in place, so the constraint is dropped and recreated.
``tag_prompt_gradients`` needs no change: patch origin travels inside the artifact's
JSON payload and has no column constraint of its own.

Revision ID: 0037_builtin_grounded
Revises: 0036_pending_sampled_sec
Create Date: 2026-08-03 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037_builtin_grounded"
down_revision: str | None = "0036_pending_sampled_sec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_tag_prompt_artifacts_compiler"
_OLD_COMPILERS = "'builtin', 'dspy_mipro', 'dspy_bootstrap', 'dspy_gepa', 'textgrad_tgd', 'manual'"
_NEW_COMPILERS = (
    "'builtin', 'builtin_grounded', 'dspy_mipro', 'dspy_bootstrap', "
    "'dspy_gepa', 'textgrad_tgd', 'manual'"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE tag_prompt_artifacts DROP CHECK {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE tag_prompt_artifacts ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (compiler IN ({_NEW_COMPILERS}))"
    )


def downgrade() -> None:
    # An artifact compiled by the grounded proposer would violate the old list, so it
    # is relabelled to the compiler whose template body it degrades to. Nothing is
    # lost: compiler_version still records that a model wrote the rules.
    op.execute(
        "UPDATE tag_prompt_artifacts SET compiler = 'builtin' WHERE compiler = 'builtin_grounded'"
    )
    op.execute(f"ALTER TABLE tag_prompt_artifacts DROP CHECK {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE tag_prompt_artifacts ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (compiler IN ({_OLD_COMPILERS}))"
    )
