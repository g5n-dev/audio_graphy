"""StreamingChunker — confirmed SegmentRecord → ChunkRecord (token budget packing).

M8 Phase 4 (WS-2 / T6). Streaming counterpart to ``core/chunker.py``'s
``_pack_chunks`` step. Accepts one SegmentRecord at a time, maintains an
in-flight buffer until ``token_budget`` is reached, then emits a
``ChunkRecord`` (reuses the batch ``ChunkRecord`` dataclass).

Design (architecture §8):
    - Same ``token_budget`` (default 1200) and ``cl100k_base`` encoding.
    - Same ``content_hash = sha256(text)`` algorithm.
    - ``overlap_tokens`` is 0 (same as batch default).
    - Per-instance state — one StreamingChunker per session.

Public API:
    push_segment(seg) -> ChunkRecord | None
        Append one confirmed segment. Returns a ChunkRecord if the buffer
        crossed the token budget, else None.
    flush() -> ChunkRecord | None
        Force-flush remaining buffer (called on session close).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

import tiktoken

from audio_graphy.core.chunker import ChunkRecord

if TYPE_CHECKING:
    from audio_graphy.core.chunker import SegmentRecord

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_BUDGET = 1200  # DESIGN.md §3.2
DEFAULT_ENCODING = "cl100k_base"


class StreamingChunker:
    """Token-budget packing for streaming confirmed segments.

    Args:
        token_budget: Max tokens per packed chunk (default 1200).
        encoding_name: tiktoken encoding (default ``cl100k_base``).
    """

    def __init__(
        self,
        *,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        encoding_name: str = DEFAULT_ENCODING,
    ) -> None:
        if token_budget < 1:
            raise ValueError(f"token_budget must be ≥ 1, got {token_budget}")
        self._token_budget = token_budget
        self._encoding_name = encoding_name
        self._enc = tiktoken.get_encoding(encoding_name)
        self._buffer_segments: list[SegmentRecord] = []
        self._buffer_tokens = 0
        self._emitted_count = 0

    @property
    def token_budget(self) -> int:
        return self._token_budget

    @property
    def emitted_count(self) -> int:
        """Number of chunks emitted so far (for metrics / audit)."""
        return self._emitted_count

    @property
    def pending_segment_count(self) -> int:
        """Segments buffered but not yet flushed."""
        return len(self._buffer_segments)

    def push_segment(self, seg: SegmentRecord) -> ChunkRecord | None:
        """Push one confirmed segment. Returns a ChunkRecord if the buffer
        reached ``token_budget``, else ``None``.

        Args:
            seg: Confirmed SegmentRecord (with transcript already attached).

        Returns:
            ChunkRecord or None.
        """
        # Skip empty transcripts entirely (consistent with batch chunker).
        if not seg.transcript or not seg.transcript.strip():
            return None

        seg_tokens = max(1, len(self._enc.encode(seg.transcript)))

        # Would adding this segment exceed budget AND we already have buffered work?
        if self._buffer_tokens + seg_tokens > self._token_budget and self._buffer_segments:
            # Flush current buffer, then start a new one with this segment.
            chunk = self._pack(self._buffer_segments)
            self._buffer_segments = [seg]
            self._buffer_tokens = seg_tokens
            self._emitted_count += 1
            return chunk

        # Otherwise accumulate.
        self._buffer_segments.append(seg)
        self._buffer_tokens += seg_tokens
        return None

    def flush(self) -> ChunkRecord | None:
        """Force-flush remaining buffer (called on session close).

        Returns:
            ChunkRecord if any segments remain in the buffer, else None.
        """
        if not self._buffer_segments:
            return None
        chunk = self._pack(self._buffer_segments)
        self._buffer_segments = []
        self._buffer_tokens = 0
        self._emitted_count += 1
        return chunk

    def reset(self) -> None:
        """Reset internal state (used after errors / reconnect)."""
        self._buffer_segments = []
        self._buffer_tokens = 0
        # Note: _emitted_count is NOT reset — it's a cumulative metric.

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _pack(self, segments: list[SegmentRecord]) -> ChunkRecord:
        """Pack a list of segments into a single ChunkRecord.

        Mirrors ``core/chunker.py:Chunker._pack_chunks`` final-pack step
        (text joined by newline, content_hash = sha256(text)).
        """
        text = "\n".join(s.transcript for s in segments)
        token_n = sum(max(1, len(self._enc.encode(s.transcript))) for s in segments if s.transcript)
        return ChunkRecord(
            segment_ids=[s.idx for s in segments],
            text=text,
            token_n=token_n,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
