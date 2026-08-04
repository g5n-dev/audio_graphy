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


_GROUNDED_VERSION = "builtin-grounded-v1"


def upgrade() -> None:
    op.execute(f"ALTER TABLE tag_prompt_artifacts DROP CHECK {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE tag_prompt_artifacts ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (compiler IN ({_NEW_COMPILERS}))"
    )
    # Restore anything a previous downgrade relabelled. ``compiler`` is inside the
    # payload ``artifact_checksum`` is computed over, so a row left saying 'builtin'
    # after a rollback disagrees with its own checksum: recompiling the identical
    # artifact then matches the stored row by checksum and reports the grounded
    # result as produced by 'builtin', and re-materializing it feeds the wrong
    # compiler into the child's checksum, breaking the documented "double submit
    # resolves to the row that already exists" idempotency across the boundary.
    # ``compiler_version`` is untouched by the downgrade, which is what makes this
    # recoverable rather than merely regrettable.
    op.execute(
        "UPDATE tag_prompt_artifacts SET compiler = 'builtin_grounded' "
        f"WHERE compiler = 'builtin' AND compiler_version = '{_GROUNDED_VERSION}'"
    )


def downgrade() -> None:
    # An artifact compiled by the grounded proposer would violate the old list, so it
    # is relabelled to the compiler whose template body it degrades to. The row is
    # left disagreeing with its own artifact_checksum for as long as the downgrade
    # holds; ``upgrade`` puts the label back from ``compiler_version``, which this
    # deliberately does not touch.
    op.execute(
        "UPDATE tag_prompt_artifacts SET compiler = 'builtin' WHERE compiler = 'builtin_grounded'"
    )
    op.execute(f"ALTER TABLE tag_prompt_artifacts DROP CHECK {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE tag_prompt_artifacts ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (compiler IN ({_OLD_COMPILERS}))"
    )
