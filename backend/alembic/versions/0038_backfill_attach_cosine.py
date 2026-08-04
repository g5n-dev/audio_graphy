"""Backfill ``attach_cosine`` from the link that recorded the same merge.

0034 added the column with ``server_default '1.0'`` and no backfill, on the
stated grounds that "their attachment confidence is unknown". That is not so:
``_merge_into_existing`` writes ``_persist_voiceprint(attach_cosine=cosine)``
and ``_persist_speaker_link(cosine_similarity=cosine)`` from the same merge with
the same number, and the link write long predates this batch. The cosine was
sitting in ``speaker_links`` the whole time.

The cost of not backfilling is that ADR-0001 never reaches an existing tenant.
SpeakerLinker ranks a speaker's vectors by ``attach_cosine >= threshold`` first
and ``duration_sec DESC`` second, so with every legacy row reading 1.0 the
leading key is true for all of them, the tie falls through to duration, and a
long tentatively-attached sample becomes the speaker's representative template
for every future comparison -- exactly the outcome the column was added to
prevent. New tenants get the fix; every tenant with an existing ambiguous merge
does not, permanently, because nothing else ever rewrites the column.

Two cases are deliberately left at 1.0 rather than guessed at:

* **Founding vectors.** A ``single_recording`` link writes ``cosine_similarity``
  NULL because no comparison happened -- the speaker was created from it. 1.0 is
  the right answer for a template that defines the speaker, not a placeholder.
* **Recordings that contributed more than one candidate.** SpeakerLinker
  explicitly expects two diarized candidates from one recording to merge into
  the same node, and the column that would tell them apart
  (``speaker_links.source_speaker_label``) only arrived in 0035 and is NULL for
  legacy rows. Where (tenant, recording, speaker) does not identify exactly one
  link, there is no non-arbitrary value, so the row keeps its default and stays
  eligible as a template. Erring toward "usable" matches the pre-0034 behaviour
  for those rows; erring the other way would silently demote templates on
  evidence we do not have.

Data-only, so ``downgrade`` is a no-op: the pre-migration values were a default
nobody chose, and restoring them would re-break template selection.

Revision ID: 0038_backfill_attach_cos
Revises: 0037_builtin_grounded
Create Date: 2026-08-04 10:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038_backfill_attach_cos"
down_revision: str | None = "0037_builtin_grounded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Correlated rather than a JOIN update: the subquery's own GROUP BY is what
    # enforces "exactly one link", so a recording that contributed two candidates
    # produces no row and the vector keeps its default.
    op.execute(
        """
        UPDATE vectors_voiceprint AS v
        SET v.attach_cosine = (
            SELECT MIN(l.cosine_similarity)
            FROM speaker_links AS l
            WHERE l.tenant_id = v.tenant_id
              AND l.recording_id = v.recording_id
              AND l.canonical_speaker_id = v.speaker_entity_id
              AND l.cosine_similarity IS NOT NULL
            HAVING COUNT(*) = 1
        )
        WHERE v.attach_cosine = 1.0
          AND EXISTS (
            SELECT 1
            FROM speaker_links AS l
            WHERE l.tenant_id = v.tenant_id
              AND l.recording_id = v.recording_id
              AND l.canonical_speaker_id = v.speaker_entity_id
              AND l.cosine_similarity IS NOT NULL
            HAVING COUNT(*) = 1
        )
        """
    )


def downgrade() -> None:
    """Deliberately empty -- see the module docstring."""
