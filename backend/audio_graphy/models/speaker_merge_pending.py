"""SpeakerMergePending ORM model — L8 fuzzy reconfirm work-queue (M9 §11.4).

When the SpeakerFuzzyMatcher (L8) finds rapidfuzz token_ratio ≥ 0.85 it
returns ``AMBIGUOUS`` and enqueues a row here for human review or for
voiceprint cosine reconfirm. Once reconfirmed (cosine ≥ 0.7) the row is
marked ``resolved_inferred``; otherwise ``resolved_rejected``.

Schema source of truth: ``docs/m9-architecture.md`` §11.4, §21.3, §10 (L8).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class SpeakerMergePending(TenantScopedBase):
    """Pending fuzzy speaker-merge decision awaiting reconfirm.

    Attributes:
        recording_id: Recording in which the ambiguous candidate surfaced.
        candidate_name: Raw text of the fuzzy-matched speaker name.
        matched_speaker_node_id: FK to ``speaker_nodes.id`` of the closest
            existing speaker (the candidate merge target).
        fuzzy_score: rapidfuzz token_ratio in [0, 1].
        status: ``pending`` / ``resolved_inferred`` / ``resolved_rejected``.
        voiceprint_score: Cosine similarity in [-1, 1] populated by the
            reconfirm pass (NULL until reconfirm runs).
        resolved_by: ``"voiceprint"`` / ``"human"`` / ``"timeout"``.
        resolved_at: When the decision was made.
        notes: Free-text reviewer notes (human path only).
    """

    __tablename__ = "speaker_merge_pending"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_name: Mapped[str] = mapped_column(String(128), nullable=False)
    matched_speaker_node_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    fuzzy_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    voiceprint_score: Mapped[float | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String(24), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resolved_inferred', 'resolved_rejected')",
            name="ck_speaker_merge_pending_status",
        ),
        CheckConstraint(
            "resolved_by IS NULL OR resolved_by IN "
            "('voiceprint', 'human', 'timeout')",
            name="ck_speaker_merge_pending_resolved_by",
        ),
        Index(
            "ix_speaker_merge_pending_tenant_status", "tenant_id", "status"
        ),
    )
