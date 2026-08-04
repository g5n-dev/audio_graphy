"""Link one recording's speakers, starting from its stored audio.

Two callers need this and neither has a ``ChunkerOutput`` to reuse:

- the backfill job, for recordings ingested before the voiceprint pipeline
  existed (``core.voiceprint_backfill``);
- streaming session finalization, which produces segments incrementally and
  never runs batch diarization (``api.ws_stream``).

Both therefore re-diarize the recording's own audio, then sample and link
exactly as the batch indexing stage does. The indexing pipeline does *not*
use this: it already diarized during chunking and passes that timeline
straight through, so re-reading the file would be wasted work.

Merged reception artifacts are never a valid ``audio_path`` here — see
docs/adr/0001-voiceprint-sampling.md.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.speaker_linker import SpeakerLinker, derive_role_hint
from audio_graphy.core.tenant_lock import tenant_advisory_lock
from audio_graphy.core.voiceprint_sampler import VoiceprintSampler
from audio_graphy.models.speaker_link import SpeakerLink

if TYPE_CHECKING:
    from audio_graphy.adapters.protocols import VoiceprintAdapter
    from audio_graphy.core.crypto import AudioCrypto

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_SEC = 60


@dataclass(frozen=True, slots=True)
class _SpeakerWindow:
    """A diarization window, shaped for VoiceprintSampler."""

    start_sec: float
    end_sec: float
    speaker: str | None


@dataclass(frozen=True, slots=True)
class RecordingLinkOutcome:
    """What happened to one recording.

    ``skipped_reason`` is set whenever ``linked`` is False, so a caller can
    always explain a no-op instead of reporting silence.
    """

    recording_id: int
    linked: bool
    new_speakers: int = 0
    merged_speakers: int = 0
    skipped_reason: str | None = None


async def link_recording_speakers(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    voiceprint: VoiceprintAdapter,
    crypto: AudioCrypto,
    settings: Any,
    tenant_id: str,
    recording_id: int,
    audio_path: str,
    recorded_at: Any = None,
    sampler: VoiceprintSampler | None = None,
    linker: SpeakerLinker | None = None,
) -> RecordingLinkOutcome:
    """Diarize, sample and link one recording. Never raises for skips.

    Args:
        sampler / linker: Pre-built collaborators. The batch job passes them
            so a run of hundreds of recordings builds each only once;
            one-shot callers leave them out.

    Returns:
        RecordingLinkOutcome. Genuine failures (unreadable audio, service
        errors) propagate — callers decide whether that is fatal.
    """
    # Off-thread: a stat() against network storage would otherwise stall the
    # caller's event loop, which for a batch run means every recording.
    if not audio_path or not await asyncio.to_thread(Path(audio_path).is_file):
        return RecordingLinkOutcome(
            recording_id=recording_id,
            linked=False,
            skipped_reason="audio file missing",
        )

    diarization = await voiceprint.diarize(audio_path)
    windows = [
        _SpeakerWindow(
            start_sec=float(seg.start_sec),
            end_sec=float(seg.end_sec),
            speaker=str(seg.speaker_id),
        )
        for seg in diarization.segments
        if seg.speaker_id and seg.end_sec > seg.start_sec
    ]
    if not windows:
        return RecordingLinkOutcome(
            recording_id=recording_id,
            linked=False,
            skipped_reason="diarization produced no speaker windows",
        )

    sampler = sampler or build_sampler(voiceprint, settings)
    linker = linker or build_linker(session_factory, crypto, settings, tenant_id)

    role_hints = derive_role_hint([(str(w.speaker), w.end_sec - w.start_sec) for w in windows])
    sample_report = await sampler.sample(
        recording_id=recording_id,
        audio_path=audio_path,
        segments=windows,
        recorded_at=recorded_at,
        role_hints=role_hints,
    )
    if not sample_report.candidates:
        return RecordingLinkOutcome(
            recording_id=recording_id,
            linked=False,
            skipped_reason=(
                f"no candidate cleared the quality gates: {sample_report.skipped_speakers}"
            ),
        )

    async with tenant_advisory_lock(
        session_factory,
        purpose="speaker_link",
        tenant_id=tenant_id,
        timeout_sec=LOCK_TIMEOUT_SEC,
        deployment_id=getattr(settings, "deployment_id", "audiography"),
    ):
        # Re-read under the lock: another path may have linked some or all of
        # these speakers while we were diarizing. Skipping per label rather
        # than per recording is what lets a run that died partway through be
        # finished by the next one.
        done = await linked_speaker_labels(session_factory, tenant_id, recording_id)
        remaining = tuple(
            candidate for candidate in sample_report.candidates if candidate.speaker_id not in done
        )
        if not remaining:
            return RecordingLinkOutcome(
                recording_id=recording_id,
                linked=False,
                skipped_reason="all speakers already linked",
            )
        if done:
            logger.info(
                "Recording %d: resuming, %d of %d speakers already linked",
                recording_id,
                len(done),
                len(sample_report.candidates),
            )
        link_report = await linker.run(recording_id, remaining)

    return RecordingLinkOutcome(
        recording_id=recording_id,
        linked=True,
        new_speakers=link_report.new_speakers,
        merged_speakers=link_report.merged_speakers,
    )


def build_sampler(voiceprint: VoiceprintAdapter, settings: Any) -> VoiceprintSampler:
    """Sampler configured from settings, so every path samples identically."""
    return VoiceprintSampler(
        voiceprint,
        strategy=settings.voiceprint_sampling_strategy,
        min_segment_sec=settings.voiceprint_sample_min_segment_sec,
        min_total_sec=settings.voiceprint_sample_min_total_sec,
        max_segments=settings.voiceprint_sample_max_segments,
        outlier_cosine=settings.voiceprint_sample_outlier_cosine,
    )


def build_linker(
    session_factory: async_sessionmaker[AsyncSession],
    crypto: AudioCrypto,
    settings: Any,
    tenant_id: str,
) -> SpeakerLinker:
    """Linker configured from settings, so every path merges identically."""
    from audio_graphy.core.speaker_fuzzy_matcher import SpeakerFuzzyMatcher

    # Built here rather than left to SpeakerLinker's argument-less default: the
    # three Layer-2 thresholds are operator settings, and GET
    # /speakers/voiceprint-policy reports them as the rules in force. Without
    # this the matcher runs its own L8 constants and the policy endpoint is
    # reporting numbers nothing consults.
    return SpeakerLinker(
        session_factory,
        crypto,
        tenant_id=tenant_id,
        voiceprint_threshold=settings.voiceprint_cosine_threshold,
        ambiguity_threshold=settings.voiceprint_ambiguous_threshold,
        enable_layer2_fuzzy=settings.enable_speaker_layer2_fuzzy,
        fuzzy_matcher=SpeakerFuzzyMatcher(
            inferred_threshold=settings.speaker_fuzzy_inferred_threshold,
            ambiguous_threshold=settings.speaker_fuzzy_ambiguous_threshold,
            reconfirm_cosine=settings.speaker_fuzzy_voiceprint_reconfirm_cosine,
        ),
    )


async def linked_speaker_labels(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    recording_id: int,
) -> set[str]:
    """Diarization labels already linked for this recording.

    Linking is not idempotent, so every entry point has to know what is
    already done. Tracking it per label rather than per recording matters:
    each candidate commits separately, so a failure partway through leaves
    some labels linked and some not. A recording-level "has any link" guard
    would then treat that recording as finished forever, and the speakers
    that never made it would be silently lost.

    Rows written before ``source_speaker_label`` existed contribute nothing
    here; they are reported separately by ``recording_is_linked``.
    """
    async with session_factory() as session:
        rows = await session.execute(
            select(SpeakerLink.source_speaker_label).where(
                SpeakerLink.tenant_id == tenant_id,
                SpeakerLink.recording_id == recording_id,
                SpeakerLink.source_speaker_label.is_not(None),
            )
        )
        return {str(label) for label in rows.scalars().all() if label}


async def recording_is_linked(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    recording_id: int,
) -> bool:
    """Whether this recording has any speaker link at all.

    Used only where per-label resolution is unavailable — chiefly for
    recordings linked before ``source_speaker_label`` existed, whose labels
    cannot be reconstructed.
    """
    async with session_factory() as session:
        found = await session.execute(
            select(SpeakerLink.id)
            .where(
                SpeakerLink.tenant_id == tenant_id,
                SpeakerLink.recording_id == recording_id,
            )
            .limit(1)
        )
        return found.scalar_one_or_none() is not None


__all__ = [
    "RecordingLinkOutcome",
    "build_linker",
    "build_sampler",
    "link_recording_speakers",
    "recording_is_linked",
]
