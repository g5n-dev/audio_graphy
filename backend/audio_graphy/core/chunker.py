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

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import VADSegment
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.segment import Segment

if TYPE_CHECKING:
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
    """

    recording_id: int
    segments: list[SegmentRecord]
    chunks: list[ChunkRecord]


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
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        token_budget: int = 1200,
        overlap_tokens: int = 0,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        file_index: FileIndex | None = None,
    ) -> None:
        self._bundle = bundle
        self._token_budget = token_budget
        self._overlap_tokens = overlap_tokens
        self._session_factory = session_factory
        self._file_index = file_index

    async def process_recording(
        self,
        recording_id: int,
        audio_path: str,
        recorded_at: datetime | None,
        *,
        tenant_id: str = "default",
    ) -> ChunkerOutput:
        """Process a recording: VAD → ASR → chunking → persistence.

        Args:
            recording_id: Database recording ID.
            audio_path: Path to the audio file.
            recorded_at: Recording timestamp (for file_index provenance).
            tenant_id: Tenant scope.

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
        segment_records = await self._transcribe_segments(vad_segments, audio_path)

        # Step 3: Pack into chunks by token budget
        chunks = self._pack_chunks(segment_records)

        # Step 4: Persist to MySQL + file_index
        if self._session_factory is not None:
            chunks = await self._persist_to_mysql(recording_id, segment_records, chunks, tenant_id)

        if self._file_index is not None:
            await self._persist_to_file_index(recording_id, segment_records, chunks, recorded_at)

        if not chunks:
            logger.warning("Recording %d produced 0 chunks (all ASR failed?)", recording_id)

        return ChunkerOutput(
            recording_id=recording_id,
            segments=segment_records,
            chunks=chunks,
        )

    # ------------------------------------------------------------------
    # ASR transcription
    # ------------------------------------------------------------------

    async def _transcribe_segments(
        self,
        vad_segments: Sequence[VADSegment],
        audio_path: str,
    ) -> list[SegmentRecord]:
        """Transcribe each VAD segment via ASR.

        ASR failures on a single segment set transcript="" but don't block
        the rest (PRD §4.1 error handling).

        Args:
            vad_segments: VAD output segments.
            audio_path: Path to the audio file (passed to ASR).

        Returns:
            List of SegmentRecord objects.
        """
        records: list[SegmentRecord] = []
        for idx, seg in enumerate(vad_segments):
            transcript = ""
            try:
                asr_result = await self._bundle.asr.transcribe(audio_path)
                transcript = asr_result.text
            except Exception as exc:
                logger.warning(
                    "ASR failed for segment %d (recording): %s — setting empty transcript",
                    idx,
                    exc,
                )

            records.append(
                SegmentRecord(
                    idx=idx,
                    start_sec=seg.start_sec,
                    end_sec=seg.end_sec,
                    transcript=transcript,
                    speaker=None,  # M2: no speaker diarization
                    vad_conf=seg.confidence,
                )
            )
        return records

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

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count for Chinese text.

        Uses len(text) // 2 approximation (Q1 decision).
        Empty strings return 0.

        Args:
            text: Input text.

        Returns:
            Estimated token count.
        """
        if not text:
            return 0
        return max(1, len(text) // 2)

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
    ) -> list[ChunkRecord]:
        """Write segments and chunks to MySQL.

        Args:
            recording_id: Recording FK.
            segments: Segment records to insert.
            chunks: Chunk records to insert (chunk_id will be set on return).
            tenant_id: Tenant scope.

        Returns:
            Updated chunk records with chunk_id set.
        """
        assert self._session_factory is not None
        updated_chunks: list[ChunkRecord] = []

        async with self._session_factory() as session:
            # Insert segments
            for seg in segments:
                session.add(
                    Segment(
                        tenant_id=tenant_id,
                        recording_id=recording_id,
                        idx=seg.idx,
                        start_sec=seg.start_sec,
                        end_sec=seg.end_sec,
                        transcript=seg.transcript if seg.transcript else None,
                        speaker=seg.speaker,
                        vad_conf=seg.vad_conf,
                    )
                )

            # Insert chunks and get IDs back
            for chunk in chunks:
                orm_chunk = Chunk(
                    tenant_id=tenant_id,
                    recording_id=recording_id,
                    segment_ids=chunk.segment_ids,
                    text=chunk.text,
                    token_n=chunk.token_n,
                    content_hash=chunk.content_hash,
                )
                session.add(orm_chunk)
                await session.flush()  # Get auto-increment ID
                updated_chunks.append(replace(chunk, chunk_id=orm_chunk.id))

            await session.commit()

        return updated_chunks if updated_chunks else chunks

    async def _persist_to_file_index(
        self,
        recording_id: int,
        segments: list[SegmentRecord],
        chunks: list[ChunkRecord],
        recorded_at: datetime | None,
    ) -> None:
        """Write segments and chunks to file_index JSON stores.

        Args:
            recording_id: Recording ID.
            segments: Segment records.
            chunks: Chunk records.
            recorded_at: Recording timestamp.
        """
        assert self._file_index is not None

        # Write video segments
        for seg in segments:
            seg_key = f"{recording_id}_{seg.idx}"
            await self._file_index.set(
                "kv_store_video_segments",
                seg_key,
                {
                    "recording_id": recording_id,
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
            chunk_key = f"{recording_id}_{chunk.chunk_id or 'pending'}"
            await self._file_index.set(
                "kv_store_text_chunks",
                chunk_key,
                {
                    "recording_id": recording_id,
                    "segment_ids": chunk.segment_ids,
                    "text": chunk.text,
                    "token_n": chunk.token_n,
                    "content_hash": chunk.content_hash,
                },
            )

        # Write video path
        await self._file_index.set(
            "kv_store_video_path",
            str(recording_id),
            {
                "recording_id": recording_id,
                "recorded_at": recorded_at.isoformat() if recorded_at else None,
            },
        )
