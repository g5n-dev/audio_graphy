"""VoiceprintVector ORM — encrypted speaker voiceprint (M7 P0-12).

PIPL §14.3 compliance:
    - Stored as AES-256-GCM envelope ciphertext via M6 ``AudioCrypto``.
    - Decryption requires the master key from ``AUDIOGRAPHY_MASTER_KEY_PATH``
      (Q3 locked: reuse M6 master key, no separate voiceprint key).
    - ``voiceprint_id`` = sha256(decrypted vector) — never the raw vector.
    - Cascade delete on DSAR / retention sweep (see ``core/retention.py``).

Table: ``vectors_voiceprint``. Inherits ``TenantScopedBase``.

M7 architecture §11.2 / §13.1.1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    import numpy as np

    from audio_graphy.core.crypto import AudioCrypto
    from audio_graphy.models.recording import Recording


def _utcnow() -> datetime:
    return datetime.now(UTC)


class VoiceprintVector(TenantScopedBase):
    """Encrypted speaker voiceprint row.

    Attributes:
        recording_id: Source recording (FK ON DELETE CASCADE).
        segment_id: Optional segment index this voiceprint was extracted from.
        speaker_entity_id: Speaker node (FK to ``speaker_nodes.id``, ON DELETE CASCADE).
            M7: speaker nodes live in a dedicated table (see SpeakerNode docstring
            for the architecture deviation note).
        voiceprint_id: sha256 hash of the decrypted vector (dedup key).
        vector_encrypted: AES-256-GCM envelope ciphertext of float32 bytes.
        encryption_meta: JSON envelope header (mirrors ``Recordings.audio_encryption_meta``).
        duration_sec: Audio duration used for extraction (quality signal).
        attach_cosine: Cosine that justified attaching this vector to its
            speaker. ``1.0`` when the vector *defines* the speaker (the node
            was created from it) or when a human confirmed the merge. A value
            below ``voiceprint_ambiguous_threshold`` marks a tentative
            attribution, which ``SpeakerLinker`` refuses to use as the
            speaker's representative template (ADR-0001). Legacy rows default
            to ``1.0``: their confidence is unknown and demoting them
            retroactively would change existing tenants' matching.
        created_at: Insertion timestamp.
    """

    __tablename__ = "vectors_voiceprint"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speaker_entity_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("speaker_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    voiceprint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vector_encrypted: Mapped[bytes] = mapped_column(LargeBinary(length=8192), nullable=False)
    encryption_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    attach_cosine: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    recording: Mapped[Recording] = relationship()

    __table_args__ = (
        Index("ix_vp_tenant_recording", "tenant_id", "recording_id"),
        Index("ix_vp_speaker", "speaker_entity_id"),
        Index(
            "ix_vp_tenant_speaker_created",
            "tenant_id",
            "speaker_entity_id",
            "created_at",
        ),
        # SpeakerLinker ranks each speaker's vectors by duration to pick a
        # representative template (ADR-0001). This index narrows the rows
        # read per tenant; it does NOT remove the sort — the ranking's
        # window function sits in a derived table that MySQL 8 always
        # materializes, and the ORDER BY mixes ascending partition keys
        # with descending sort keys, so a filesort remains.
        Index(
            "ix_vp_tenant_speaker_duration",
            "tenant_id",
            "speaker_entity_id",
            "duration_sec",
        ),
        Index("ux_vp_voiceprint_id", "tenant_id", "voiceprint_id", unique=True),
    )

    def decrypted_vector(self, crypto: AudioCrypto) -> np.ndarray:
        """Decrypt + parse to 192-d numpy array.

        Args:
            crypto: ``AudioCrypto`` instance bound to the master key.

        Returns:
            ``np.ndarray`` of dtype float32, shape (192,).
        """
        import numpy as np

        plaintext = crypto.decrypt_bytes(self.vector_encrypted, self.encryption_meta)
        return np.frombuffer(plaintext, dtype=np.float32)

    def __repr__(self) -> str:
        return (
            f"<VoiceprintVector id={self.id} "
            f"voiceprint_id={self.voiceprint_id[:16]}... "
            f"speaker_entity_id={self.speaker_entity_id}>"
        )


__all__ = ["VoiceprintVector"]
