"""SpeakerNode ORM — cross-recording speaker entity (M7 P0-9 / P0-10).

PIPL §14.3 + M7 architecture §7.

Deviation note (round 1):
    The architecture spec §7 says "speaker nodes live in the existing
    ``entities`` table with ``entity_type='SPEAKER'``". The reality is that
    AudioGraphy stores entities ONLY in NetworkX GraphML files — there is
    NO MySQL ``entities`` table. FK references in architecture §13.1.1
    (``speaker_entity_id → entities.id``) therefore cannot be implemented
    verbatim.

    To preserve the cascade-delete + tenant-isolation + voiceprint-binding
    semantics that the architecture requires, M7 introduces ``speaker_nodes``
    as a real MySQL table (this ORM). The NetworkX graph layer still
    exposes speaker nodes with the same attribute schema (§7.1), but the
    authoritative storage for voiceprint linkage is this table.

    This is the cleanest fix: NetworkX GraphML files are mutable / rebuilt
    on demand, while voiceprint data is PIPL-regulated immutable evidence
    that must survive graph rebuilds. Storing them in different layers
    (NetworkX for graph queries, MySQL for cascade + voiceprint binding)
    matches the existing project split (vectors_entity references NetworkX
    entity_id strings, not a MySQL FK).

Table: ``speaker_nodes``. Inherits ``TenantScopedBase``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase

_VALID_ROLES = ("agent", "customer", "unknown")
_VALID_STRATEGIES = (
    "voiceprint",
    "fuzzy",
    "manual",
    "single_recording",
)
_VALID_AMBIGUITY = (None, "", "AMBIGUOUS", "PENDING_REVIEW")


class SpeakerNode(TenantScopedBase):
    """Speaker entity that aggregates one speaker across multiple recordings.

    Attributes:
        voiceprint_id: sha256 hash of the decrypted voiceprint vector.
            Acts as the cross-recording link key. Two SpeakerNodes with
            the same ``voiceprint_id`` (within a tenant) MUST be merged.
        display_name: Human-readable name (e.g. ``"speaker:vp_a1b2c3d4"``).
        speaker_role: ``"agent"`` / ``"customer"`` / ``"unknown"``.
        recordings_list: JSON list of recording IDs this speaker appears in.
        recordings_count: Derived length of ``recordings_list`` (denormalized).
        first_seen: Earliest recording ``recorded_at`` timestamp.
        total_speech_sec: Cumulative speech duration across all recordings.
        merge_confidence: Highest merge confidence observed (0.0–1.0).
        merge_strategy: How the speaker was last merged.
        ambiguity_tag: ``None`` / ``"AMBIGUOUS"`` / ``"PENDING_REVIEW"``.
        attrs: Free-form JSON for additional NetworkX-node-style properties
            (e.g. ``source_ids`` / ``voiceprint_vector_hash`` for visualization).
    """

    __tablename__ = "speaker_nodes"

    voiceprint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    speaker_role: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    recordings_list: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    recordings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_speech_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    merge_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    merge_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="single_recording"
    )
    ambiguity_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            f"speaker_role IN {_VALID_ROLES}",
            name="ck_speaker_nodes_role",
        ),
        CheckConstraint(
            f"merge_strategy IN {_VALID_STRATEGIES}",
            name="ck_speaker_nodes_strategy",
        ),
        CheckConstraint(
            "ambiguity_tag IS NULL OR ambiguity_tag IN ('AMBIGUOUS', 'PENDING_REVIEW')",
            name="ck_speaker_nodes_ambiguity",
        ),
        Index("ux_speaker_nodes_vp", "tenant_id", "voiceprint_id", unique=True),
        Index("ix_speaker_nodes_role", "tenant_id", "speaker_role"),
    )

    def __repr__(self) -> str:
        return (
            f"<SpeakerNode id={self.id} "
            f"voiceprint={self.voiceprint_id[:16]}... "
            f"role={self.speaker_role} recordings={self.recordings_count}>"
        )


__all__ = ["SpeakerNode"]
