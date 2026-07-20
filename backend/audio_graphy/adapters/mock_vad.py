"""Mock VAD adapter — deterministic voice activity detection.

Strategy:
- Reads audio file size as a seed
- Returns segments at fixed cadence (~6-15s each) so downstream chunking is predictable
- Skips a few short "silent" gaps to mimic real VAD behavior
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from collections.abc import Sequence

from audio_graphy.adapters.protocols import VADSegment

logger = logging.getLogger(__name__)


class MockVADAdapter:
    """Deterministic mock VAD — same file always yields same segments."""

    def __init__(self, *, latency_ms: int = 50) -> None:
        self._latency_ms = latency_ms

    async def segment(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = 0.5,
        max_segment_sec: float = 30.0,
    ) -> Sequence[VADSegment]:
        # Simulate network/service latency
        await asyncio.sleep(self._latency_ms / 1000.0)

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Seed RNG with file hash for determinism
        file_size = os.path.getsize(audio_path)
        file_hash = hashlib.md5(f"{audio_path}:{file_size}".encode()).hexdigest()
        seed = int(file_hash[:8], 16)
        rng = random.Random(seed)

        # Pretend total duration scales with file size (~100KB/sec for compressed audio)
        total_duration = max(5.0, file_size / 100_000.0)
        if total_duration > 7200:  # cap absurd values
            total_duration = 7200.0

        segments: list[VADSegment] = []
        cursor = 0.0
        while cursor < total_duration:
            seg_len = rng.uniform(6.0, min(max_segment_sec, 15.0))
            if cursor + seg_len > total_duration:
                seg_len = total_duration - cursor
            if seg_len >= min_segment_sec:
                segments.append(
                    VADSegment(
                        start_sec=round(cursor, 3),
                        end_sec=round(cursor + seg_len, 3),
                        confidence=round(rng.uniform(0.85, 0.99), 3),
                    )
                )
            # Skip a short silent gap sometimes
            cursor += seg_len + rng.uniform(0.0, 0.8)

        logger.debug("Mock VAD returned %d segments for %s", len(segments), audio_path)
        return segments
