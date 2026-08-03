"""Backfill speaker voiceprints for recordings ingested before ADR-0001.

Turning on ``enable_voiceprint`` only affects recordings indexed from that
point on. Everything already in the database was chunked with diarization
off, so its segments carry no speaker labels and it will never gain a
cross-recording identity — which is most of the value of speaker linking
for an existing tenant.

This job closes that gap. Unlike the indexing stage it cannot reuse
``ChunkerOutput``: those recordings were never diarized, so it re-runs
diarization on the stored audio, then samples and links exactly as the
live pipeline does.

Cost warning: diarization reads the whole file. This is a batch job, meant
to be run deliberately (``scripts/backfill_voiceprints.py``) or from a
low-traffic cron — not on the request path.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.recording_speaker_link import (
    build_linker,
    build_sampler,
    link_recording_speakers,
)
from audio_graphy.core.speaker_linker import SpeakerLinker
from audio_graphy.core.voiceprint_sampler import VoiceprintSampler
from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.recording import Recording
from audio_graphy.models.speaker_link import SpeakerLink

if TYPE_CHECKING:
    from audio_graphy.adapters.protocols import VoiceprintAdapter
    from audio_graphy.core.crypto import AudioCrypto

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BackfillReport:
    """Outcome of one backfill pass.

    Attributes:
        tenant_id: Tenant that was scanned.
        scanned: Recordings considered.
        linked: Recordings that produced at least one speaker link.
        new_speakers / merged_speakers: Totals across all linked recordings.
        skipped: ``recording_id -> reason`` for everything not linked, so a
            run that achieves nothing still says why.
        last_scanned_id: Highest recording id examined. Pass it back as
            ``after_id`` to continue past recordings that can never link.
    """

    tenant_id: str
    scanned: int = 0
    linked: int = 0
    new_speakers: int = 0
    merged_speakers: int = 0
    skipped: dict[int, str] = field(default_factory=dict)
    last_scanned_id: int = 0


class VoiceprintBackfill:
    """Re-diarize and link historical recordings that have no speaker links.

    Args:
        session_factory: Async session maker.
        voiceprint: CAM++ adapter (diarize + extract).
        crypto: Encrypts voiceprint vectors at rest.
        settings: Supplies the sampling gates and linker thresholds, so a
            backfill can never disagree with the live pipeline.
        tenant_id: Tenant to scan. One instance per tenant.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        voiceprint: VoiceprintAdapter,
        crypto: AudioCrypto,
        settings: Any,
        *,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._voiceprint = voiceprint
        self._crypto = crypto
        self._settings = settings
        self._tenant_id = tenant_id

    async def run(self, *, limit: int = 20, after_id: int = 0) -> BackfillReport:
        """Backfill up to ``limit`` unlinked recordings.

        Bounded on purpose: each recording costs a full-file diarization
        plus several extractions, so the caller controls how much work one
        pass does and can resume by simply running again.
        """
        if limit < 1:
            raise ValueError("limit must be ≥ 1")

        report = BackfillReport(tenant_id=self._tenant_id)
        candidates = await self.pending_recordings(limit=limit, after_id=after_id)
        if not candidates:
            logger.info("Backfill: tenant %s has no unlinked recordings", self._tenant_id)
            return report

        # Built once for the whole pass rather than per recording.
        sampler = build_sampler(self._voiceprint, self._settings)
        linker = build_linker(self._session_factory, self._crypto, self._settings, self._tenant_id)

        for recording_id, audio_path, recorded_at in candidates:
            report.scanned += 1
            report.last_scanned_id = max(report.last_scanned_id, recording_id)
            try:
                await self._backfill_one(
                    recording_id=recording_id,
                    audio_path=audio_path,
                    recorded_at=recorded_at,
                    sampler=sampler,
                    linker=linker,
                    report=report,
                )
            except Exception as exc:
                # One unreadable file or one service hiccup must not end the
                # pass — the remaining recordings are independent.
                logger.warning(
                    "Backfill failed for recording %d: %s", recording_id, exc, exc_info=True
                )
                report.skipped[recording_id] = f"failed: {exc}"

        logger.info(
            "Backfill for tenant %s: scanned=%d linked=%d new=%d merged=%d skipped=%d",
            self._tenant_id,
            report.scanned,
            report.linked,
            report.new_speakers,
            report.merged_speakers,
            len(report.skipped),
        )
        return report

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _backfill_one(
        self,
        *,
        recording_id: int,
        audio_path: str,
        recorded_at: Any,
        sampler: VoiceprintSampler,
        linker: SpeakerLinker,
        report: BackfillReport,
    ) -> None:
        outcome = await link_recording_speakers(
            session_factory=self._session_factory,
            voiceprint=self._voiceprint,
            crypto=self._crypto,
            settings=self._settings,
            tenant_id=self._tenant_id,
            recording_id=recording_id,
            audio_path=audio_path,
            recorded_at=recorded_at,
            sampler=sampler,
            linker=linker,
        )
        if not outcome.linked:
            report.skipped[recording_id] = outcome.skipped_reason or "not linked"
            return
        report.linked += 1
        report.new_speakers += outcome.new_speakers
        report.merged_speakers += outcome.merged_speakers

    async def pending_recordings(
        self,
        *,
        limit: int,
        after_id: int = 0,
    ) -> Sequence[tuple[int, str, Any]]:
        """Indexed recordings in this tenant with no speaker links, oldest first.

        ``after_id`` is what makes repeated runs progress. Plenty of
        recordings can never produce a link — their audio was deleted by the
        retention sweep, diarization finds no speech, nobody clears the
        quality gates — and those never gain a SpeakerLink. Without a cursor
        the same unlinkable oldest rows fill every batch forever and nothing
        beyond them is ever examined.
        """
        linked = select(SpeakerLink.recording_id).where(SpeakerLink.tenant_id == self._tenant_id)
        stmt = (
            select(Recording.id, Recording.path, Recording.recorded_at)
            .where(
                Recording.tenant_id == self._tenant_id,
                # Only recordings that finished indexing. Queued and failed
                # ones will be linked by the pipeline when they complete, and
                # diarizing a verified-silent recording just burns a
                # full-file inference to find nothing.
                Recording.status == RecordingStatus.INDEXED.value,
                Recording.id > after_id,
                Recording.id.notin_(linked),
            )
            .order_by(Recording.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [(int(r[0]), str(r[1] or ""), r[2]) for r in rows]


__all__ = ["BackfillReport", "VoiceprintBackfill"]
