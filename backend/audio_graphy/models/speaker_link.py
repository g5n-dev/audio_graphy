"""SpeakerLink ORM — audit-trail of cross-recording speaker merges (M7 P0-11).

Each row records one SpeakerLinker merge decision: which source speaker
(from recording X) was merged into which canonical SpeakerNode, with what
confidence and strategy. This is the audit / undo trail; the live state
of ``SpeakerNode.recordings_list`` is the materialised view.

Table: ``speaker_links``. Inherits ``TenantScopedBase``.

M7 architecture §8.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase

_VALID_STRATEGIES = ("voiceprint", "fuzzy", "manual", "single_recording")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpeakerLink(TenantScopedBase):
    """One row per speaker-link decision (append-only audit trail).

    Attributes:
        canonical_speaker_id: FK to ``speaker_nodes.id`` — the surviving node.
        source_speaker_id: FK to ``speaker_nodes.id`` of the merged-away node.
            For ``strategy='single_recording'`` (no merge), this equals
            ``canonical_speaker_id`` and the row just records "new speaker
            created from recording X".
        recording_id: The recording that triggered this decision (FK CASCADE).
        cosine_similarity: Voiceprint cosine for ``strategy='voiceprint'``.
            NULL otherwise.
        merge_confidence: Effective confidence used for the merge.
        strategy: ``voiceprint`` / ``fuzzy`` / ``manual`` / ``single_recording``.
        ambiguity_tag: ``"AMBIGUOUS"`` for borderline voiceprint merges,
            NULL for high-confidence or ``single_recording`` rows.
        decided_at: When the merge was decided (audit chronology).
    """

    __tablename__ = "speaker_links"

    canonical_speaker_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_speaker_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    cosine_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    merge_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    ambiguity_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            f"strategy IN {_VALID_STRATEGIES}",
            name="ck_speaker_links_strategy",
        ),
        CheckConstraint(
            "ambiguity_tag IS NULL OR ambiguity_tag IN ('AMBIGUOUS', 'PENDING_REVIEW')",
            name="ck_speaker_links_ambiguity",
        ),
        Index("ix_speaker_links_canonical", "canonical_speaker_id"),
        Index("ix_speaker_links_source", "source_speaker_id"),
        Index("ix_speaker_links_recording", "recording_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SpeakerLink id={self.id} "
            f"canonical={self.canonical_speaker_id} "
            f"source={self.source_speaker_id} "
            f"strategy={self.strategy} conf={self.merge_confidence:.2f}>"
        )


__all__ = ["SpeakerLink"]
