"""Chunker — VAD → ASR → token-budget chunking with 3-level provenance.

Pipeline:
    1. VAD: ``bundle.vad.segment(audio_path)`` → VADSegment[]
    2. ASR: ``bundle.asr.transcribe(audio_path)`` per segment → ASRResult
    3. Pack: accumulate segment transcripts by token_budget → ChunkRecord[]
    4. Persist: write segments + chunks to MySQL and file_index (if provided)

Token estimation: ``len(text) // 2`` (Q1 decision — Chinese char count / 2 approximation).

Error handling (PRD §4.1):
    - VAD adapter failure: propagates (caller sets recording.status=failed)
    - ASR single-segment failure: transcript="", continue processing
    - All segments ASR fail: return empty chunks (caller decides)
    - File not found: FileNotFoundError propagates
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

import tiktoken
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import DiarizationSegment, VADSegment
from audio_graphy.models.chunk import Chunk, ChunkSegment
from audio_graphy.models.segment import Segment

if TYPE_CHECKING:
    from audio_graphy.core.pii import PIIScrubber
    from audio_graphy.storage.file_index import FileIndex

logger = logging.getLogger(__name__)


# ============================================================
# Data classes (frozen + slots, per architecture §7.2)
# ============================================================


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    """VAD + ASR output for a single segment.

    Attributes:
        idx: Segment index within the recording (0-based, unique per recording).
        start_sec: Segment start time in seconds.
        end_sec: Segment end time in seconds.
        transcript: ASR transcript text (empty string if ASR failed).
        speaker: Speaker label (None in M2 — no speaker diarization).
        vad_conf: VAD confidence score.
    """

    idx: int
    start_sec: float
    end_sec: float
    transcript: str
    speaker: str | None
    vad_conf: float


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """A packed text chunk with provenance to source segments.

    Attributes:
        segment_ids: List of segment indices that were packed into this chunk.
        text: Concatenated transcript text.
        token_n: Estimated token count (len(text) // 2).
        content_hash: SHA-256(text) for idempotent deduplication.
        chunk_id: Database-assigned chunk ID (set after MySQL INSERT, None before).
    """

    segment_ids: list[int]
    text: str
    token_n: int
    content_hash: str
    chunk_id: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkerOutput:
    """Complete output of the chunker pipeline.

    Attributes:
        recording_id: The recording that was processed.
        segments: All segment records.
        chunks: All chunk records (with chunk_id set if MySQL write succeeded).
        diarization: The raw CAM++ speaker timeline, empty when diarization
            did not run. Kept separate from ``segments`` because the two use
            different boundaries: a VAD segment is cut on silence and can span
            a speaker change, carrying only the label of whoever owns its
            midpoint. Voiceprint sampling must crop on these boundaries
            instead, or it feeds another person's speech into the embedding.
    """

    recording_id: int
    segments: list[SegmentRecord]
    chunks: list[ChunkRecord]
    diarization: tuple[DiarizationSegment, ...] = ()


# ============================================================
# Chunker
# ============================================================


class Chunker:
    """VAD → ASR → token-budget chunking pipeline.

    Args:
        bundle: AdapterBundle (uses vad + asr adapters).
        token_budget: Maximum tokens per chunk (default 1200, DESIGN.md §3.2).
        overlap_tokens: Overlap tokens between chunks (default 0, streaming reserved).
        session_factory: Optional async session factory for MySQL writes.
        file_index: Optional FileIndex for working_dir JSON writes.
        enable_voiceprint: When ``True`` AND ``bundle.voiceprint`` is set,
            Chunker calls CAM++ diarize once per file and tags each VAD
            segment with the matching speaker_id (replacing the M3-M6
            ``speaker=None`` hardcode). When ``False`` (default), all M3-M6
            tests run unchanged (speaker is always None).
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        token_budget: int = 1200,
        overlap_tokens: int = 0,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        file_index: FileIndex | None = None,
        encoding_name: str = "cl100k_base",
        enable_voiceprint: bool = False,
        pii_scrubber: PIIScrubber | None = None,
    ) -> None:
        self._bundle = bundle
        self._token_budget = token_budget
        self._overlap_tokens = overlap_tokens
        self._session_factory = session_factory
        self._file_index = file_index
        self._encoding_name = encoding_name
        self._enc = tiktoken.get_encoding(encoding_name)
        self._enable_voiceprint = enable_voiceprint
        self._pii_scrubber = pii_scrubber

    async def process_recording(
        self,
        recording_id: int,
        audio_path: str,
        recorded_at: datetime | None,
        *,
        tenant_id: str = "default",
        pipeline_run_id: int | None = None,
        generation: int = 0,
    ) -> ChunkerOutput:
        """Process a recording: VAD → ASR → chunking → persistence.

        Args:
            recording_id: Database recording ID.
            audio_path: Path to the audio file.
            recorded_at: Recording timestamp (for file_index provenance).
            tenant_id: Tenant scope.
            pipeline_run_id: Immutable processing attempt owning new rows.
            generation: Per-recording processing generation.

        Returns:
            ChunkerOutput with segments and chunks.

        Raises:
            FileNotFoundError: If audio_path doesn't exist.
            Exception: VAD adapter failures propagate.
        """
        logger.info("Processing recording %d from %s", recording_id, audio_path)

        # Step 1: VAD segmentation
        vad_segments = await self._bundle.vad.segment(audio_path)
        logger.debug("VAD returned %d segments for recording %d", len(vad_segments), recording_id)

        # Step 2: ASR per segment
        segment_records, diar_timeline = await self._transcribe_segments(vad_segments, audio_path)

        # Raw ASR text exists only in memory. Redact before chunk construction,
        # database persistence, embeddings, graph extraction or file-index
        # fallback can observe it.
        segment_records = self._scrub_segments(segment_records)

        # Step 3: Pack into chunks by token budget
        chunks = self._pack_chunks(segment_records)

        # Step 4: Persist to MySQL + file_index
        if self._session_factory is not None:
            chunks = await self._persist_to_mysql(
                recording_id,
                segment_records,
                chunks,
                tenant_id,
                pipeline_run_id=pipeline_run_id,
                generation=generation,
            )

        if self._file_index is not None:
            await self._persist_to_file_index(
                recording_id,
                segment_records,
                chunks,
                recorded_at,
                generation=generation,
            )

        if not chunks:
            logger.warning("Recording %d produced 0 chunks (all ASR failed?)", recording_id)

        return ChunkerOutput(
            recording_id=recording_id,
            segments=segment_records,
            chunks=chunks,
            diarization=tuple(diar_timeline),
        )

    def _scrub_segments(
        self,
        segments: list[SegmentRecord],
    ) -> list[SegmentRecord]:
        """Return persistence-safe segments while preserving timing metadata."""
        if self._pii_scrubber is None:
            return segments
        return [
            replace(
                segment,
                transcript=self._pii_scrubber.scrub_simple(segment.transcript),
            )
            for segment in segments
        ]

    # ------------------------------------------------------------------
    # ASR transcription
    # ------------------------------------------------------------------

    async def _transcribe_segments(
        self,
        vad_segments: Sequence[VADSegment],
        audio_path: str,
    ) -> tuple[list[SegmentRecord], list[DiarizationSegment]]:
        """Transcribe each VAD segment via ASR, optionally tag speaker via diarization.

        M7: When ``enable_voiceprint=True`` and ``bundle.voiceprint`` is set,
        Chunker calls CAM++ ``diarize`` once on the full audio, then matches
        each VAD segment to its overlapping speaker. When ``enable_voiceprint=False``
        (default), ``speaker`` is ``None`` for every segment — exactly the
        M3-M6 behaviour.

        ASR failures on a single segment set transcript="" but don't block
        the rest (PRD §4.1 error handling). Diarization failures fall back
        to ``speaker=None`` for the whole file (warning logged).

        Args:
            vad_segments: VAD output segments.
            audio_path: Path to the audio file (passed to ASR + CAM++).

        Returns:
            ``(segment records, diarization timeline)``. The timeline is
            empty when diarization is disabled or failed; it is returned
            rather than discarded because voiceprint sampling needs the
            speaker boundaries, not the VAD ones.
        """
        # Optional diarization — M7 enable_voiceprint flag gate.
        diar_timeline: list[DiarizationSegment] = []
        if self._enable_voiceprint and self._bundle.voiceprint is not None:
            try:
                diar = await self._bundle.voiceprint.diarize(audio_path)
                diar_timeline = list(diar.segments)
            except Exception as exc:
                logger.warning(
                    "Diarization failed for %s, falling back to speaker=None: %s",
                    audio_path,
                    exc,
                )
                diar_timeline = []

        records: list[SegmentRecord] = []
        for idx, seg in enumerate(vad_segments):
            transcript = ""
            try:
                # Pass the single VAD segment to ASR (W12: segment-level transcription)
                single_segment = [seg] if seg else None
                asr_result = await self._bundle.asr.transcribe(
                    audio_path,
                    segments=single_segment,
                )
                transcript = asr_result.text
            except Exception as exc:
                logger.warning(
                    "ASR failed for segment %d (recording): %s — setting empty transcript",
                    idx,
                    exc,
                )

            # M7: speaker assignment via diarization timeline overlap.
            # When diarization is disabled or empty, speaker remains None
            # (preserves M3-M6 back-compat — grep `speaker=None` returns
            # only this fallback branch + the enable_voiceprint=False default).
            speaker_id = self._match_speaker(seg, diar_timeline)

            records.append(
                SegmentRecord(
                    idx=idx,
                    start_sec=seg.start_sec,
                    end_sec=seg.end_sec,
                    transcript=transcript,
                    speaker=speaker_id,  # M7: no longer hard-coded None
                    vad_conf=seg.confidence,
                )
            )
        return records, diar_timeline

    @staticmethod
    def _match_speaker(
        vad_seg: VADSegment,
        timeline: list[DiarizationSegment],
    ) -> str | None:
        """Match VAD segment to diarization speaker via midpoint, then max-overlap.

        Strategy:
            1. Compute VAD midpoint; if a diarization segment contains it, return its speaker_id.
            2. Otherwise pick the diarization segment with max time-overlap.
            3. Empty timeline → ``None`` (preserves enable_voiceprint=False behaviour).
        """
        if not timeline:
            return None
        midpoint = (vad_seg.start_sec + vad_seg.end_sec) / 2.0
        for d in timeline:
            if d.start_sec <= midpoint <= d.end_sec:
                return d.speaker_id
        # No midpoint match — pick max overlap.
        best_spk: str | None = None
        best_overlap = 0.0
        for d in timeline:
            ov = min(vad_seg.end_sec, d.end_sec) - max(vad_seg.start_sec, d.start_sec)
            if ov > best_overlap:
                best_overlap = ov
                best_spk = d.speaker_id
        return best_spk

    # ------------------------------------------------------------------
    # Token budget packing
    # ------------------------------------------------------------------

    def _pack_chunks(self, segments: list[SegmentRecord]) -> list[ChunkRecord]:
        """Pack segment transcripts into chunks by token budget.

        Algorithm:
            1. Iterate through segments in order.
            2. Accumulate transcripts into the current chunk.
            3. When token count exceeds token_budget, start a new chunk.
            4. Each chunk records the segment indices it contains.

        Args:
            segments: All segment records.

        Returns:
            List of ChunkRecord objects (without chunk_id — set after DB insert).
        """
        if not segments:
            return []

        chunks: list[ChunkRecord] = []
        current_text: list[str] = []
        current_segment_ids: list[int] = []
        current_tokens = 0

        for seg in segments:
            seg_tokens = self._estimate_tokens(seg.transcript)
            if seg_tokens == 0:
                continue  # Skip empty transcripts

            # Check if adding this segment would exceed the budget
            if current_tokens + seg_tokens > self._token_budget and current_text:
                # Flush current chunk
                text = "\n".join(current_text)
                chunks.append(
                    ChunkRecord(
                        segment_ids=list(current_segment_ids),
                        text=text,
                        token_n=current_tokens,
                        content_hash=self._compute_content_hash(text),
                    )
                )
                # Start new chunk
                current_text = []
                current_segment_ids = []
                current_tokens = 0

            current_text.append(seg.transcript)
            current_segment_ids.append(seg.idx)
            current_tokens += seg_tokens

        # Flush remaining
        if current_text:
            text = "\n".join(current_text)
            chunks.append(
                ChunkRecord(
                    segment_ids=list(current_segment_ids),
                    text=text,
                    token_n=current_tokens,
                    content_hash=self._compute_content_hash(text),
                )
            )

        return chunks

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken cl100k_base encoding.

        Replaces the old ``len(text) // 2`` approximation (W11 upgrade).
        Handles Chinese/English mixed text accurately.

        Uses lazy initialization of the tiktoken encoder to support
        instances created via ``__new__`` (bypassing ``__init__``).

        Args:
            text: Input text.

        Returns:
            Token count (0 for empty strings).
        """
        if not text:
            return 0
        enc = getattr(self, "_enc", None)
        if enc is None:
            enc = tiktoken.get_encoding(getattr(self, "_encoding_name", "cl100k_base"))
            self._enc = enc
        return max(1, len(enc.encode(text)))

    @staticmethod
    def _compute_content_hash(text: str) -> str:
        """Compute SHA-256 hash of text for idempotent deduplication.

        Args:
            text: Chunk text.

        Returns:
            Hex digest string.
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_to_mysql(
        self,
        recording_id: int,
        segments: list[SegmentRecord],
        chunks: list[ChunkRecord],
        tenant_id: str,
        *,
        pipeline_run_id: int | None = None,
        generation: int = 0,
    ) -> list[ChunkRecord]:
        """Write segments and chunks to MySQL.

        Args:
            recording_id: Recording FK.
            segments: Segment records to insert.
            chunks: Chunk records to insert (chunk_id will be set on return).
            tenant_id: Tenant scope.
            pipeline_run_id: Immutable processing attempt owning the rows.
            generation: Per-recording processing generation.

        Returns:
            Updated chunk records with chunk_id set.
        """
        assert self._session_factory is not None
        updated_chunks: list[ChunkRecord] = []

        async with self._session_factory() as session:
            # A retry of the same inactive generation replaces only its own
            # staging rows. It never mutates a previous active generation.
            if pipeline_run_id is not None:
                await session.execute(
                    delete(ChunkSegment).where(
                        ChunkSegment.pipeline_run_id == pipeline_run_id,
                        ChunkSegment.tenant_id == tenant_id,
                    )
                )
                await session.execute(
                    delete(Chunk).where(
                        Chunk.pipeline_run_id == pipeline_run_id,
                        Chunk.tenant_id == tenant_id,
                    )
                )
                await session.execute(
                    delete(Segment).where(
                        Segment.pipeline_run_id == pipeline_run_id,
                        Segment.tenant_id == tenant_id,
                    )
                )

            # Insert segments
            persisted_segments: dict[int, Segment] = {}
            for seg in segments:
                orm_segment = Segment(
                    tenant_id=tenant_id,
                    recording_id=recording_id,
                    pipeline_run_id=pipeline_run_id,
                    generation=generation,
                    idx=seg.idx,
                    start_sec=seg.start_sec,
                    end_sec=seg.end_sec,
                    transcript=seg.transcript if seg.transcript else None,
                    text_scrubbed=seg.transcript if seg.transcript else None,
                    speaker=seg.speaker,
                    vad_conf=seg.vad_conf,
                )
                session.add(orm_segment)
                persisted_segments[seg.idx] = orm_segment
            await session.flush()

            # Insert chunks and get IDs back
            for ordinal, chunk in enumerate(chunks):
                segment_rows = [persisted_segments[idx] for idx in chunk.segment_ids]
                persisted_segment_ids = [int(segment.id) for segment in segment_rows]
                orm_chunk = Chunk(
                    tenant_id=tenant_id,
                    recording_id=recording_id,
                    pipeline_run_id=pipeline_run_id,
                    generation=generation,
                    ordinal=ordinal,
                    segment_ids=persisted_segment_ids,
                    text=chunk.text,
                    token_n=chunk.token_n,
                    content_hash=chunk.content_hash,
                )
                session.add(orm_chunk)
                await session.flush()  # Get auto-increment ID
                for provenance_ordinal, segment_id in enumerate(persisted_segment_ids):
                    session.add(
                        ChunkSegment(
                            tenant_id=tenant_id,
                            recording_id=recording_id,
                            pipeline_run_id=pipeline_run_id,
                            generation=generation,
                            chunk_id=int(orm_chunk.id),
                            segment_id=segment_id,
                            ordinal=provenance_ordinal,
                        )
                    )
                updated_chunks.append(replace(chunk, chunk_id=orm_chunk.id))

            await session.commit()

        return updated_chunks if updated_chunks else chunks

    async def _persist_to_file_index(
        self,
        recording_id: int,
        segments: list[SegmentRecord],
        chunks: list[ChunkRecord],
        recorded_at: datetime | None,
        *,
        generation: int = 0,
    ) -> None:
        """Write segments and chunks to file_index JSON stores.

        Args:
            recording_id: Recording ID.
            segments: Segment records.
            chunks: Chunk records.
            recorded_at: Recording timestamp.
            generation: Per-recording processing generation.
        """
        assert self._file_index is not None

        # Write video segments
        for seg in segments:
            seg_key = (
                f"{recording_id}_g{generation}_{seg.idx}"
                if generation > 0
                else f"{recording_id}_{seg.idx}"
            )
            await self._file_index.set(
                "kv_store_video_segments",
                seg_key,
                {
                    "recording_id": recording_id,
                    "generation": generation,
                    "idx": seg.idx,
                    "start_sec": seg.start_sec,
                    "end_sec": seg.end_sec,
                    "transcript": seg.transcript,
                    "speaker": seg.speaker,
                    "vad_conf": seg.vad_conf,
                    "recorded_at": recorded_at.isoformat() if recorded_at else None,
                },
            )

        # Write text chunks
        for chunk in chunks:
            chunk_key = (
                f"{recording_id}_g{generation}_{chunk.chunk_id or 'pending'}"
                if generation > 0
                else f"{recording_id}_{chunk.chunk_id or 'pending'}"
            )
            await self._file_index.set(
                "kv_store_text_chunks",
                chunk_key,
                {
                    "recording_id": recording_id,
                    "generation": generation,
                    "segment_ids": chunk.segment_ids,
                    "text": chunk.text,
                    "token_n": chunk.token_n,
                    "content_hash": chunk.content_hash,
                },
            )

        # Write video path
        video_path_key = f"{recording_id}_g{generation}" if generation > 0 else str(recording_id)
        await self._file_index.set(
            "kv_store_video_path",
            video_path_key,
            {
                "recording_id": recording_id,
                "generation": generation,
                "recorded_at": recorded_at.isoformat() if recorded_at else None,
            },
        )
