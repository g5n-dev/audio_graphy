"""Mock embedding adapter — deterministic hash-based vectors.

Strategy:
- For each input text, produce a deterministic 1024-dim vector
- Same text → same vector (cache-friendly, retrieval reproducible)
- Different texts → different vectors (cosine sim varies, but close for similar text)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import struct
from collections.abc import Sequence

from audio_graphy.adapters.protocols import EmbeddingResult

logger = logging.getLogger(__name__)


class MockEmbedAdapter:
    """Deterministic mock embedding via MD5-seeded pseudo-random vectors."""

    def __init__(
        self,
        *,
        dim: int = 1024,
        model: str = "mock-bge-m3",
        latency_ms: int = 30,
    ) -> None:
        if dim <= 0 or dim % 8 != 0:
            raise ValueError(f"dim must be positive multiple of 8, got {dim}")
        self.dim = dim
        self.model = model
        self._latency_ms = latency_ms
        self._call_count = 0

    async def embed_texts(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        self._call_count += 1
        await asyncio.sleep(self._latency_ms / 1000.0)

        results: list[EmbeddingResult] = []
        for text in texts:
            vector = self._hash_to_vector(text)
            results.append(
                EmbeddingResult(
                    vector=vector,
                    dim=self.dim,
                    model=self.model,
                )
            )

        logger.debug(
            "Mock embed call #%d: %d texts × %d dim",
            self._call_count,
            len(texts),
            self.dim,
        )
        return results

    def _hash_to_vector(self, text: str) -> tuple[float, ...]:
        """Deterministic 1024-dim vector from text hash.

        Uses repeated MD5 to fill the vector. Each text gets a unique but
        deterministic vector — cosine similarity between two texts depends
        only on their hash distance, not their semantic content.
        """
        # Generate enough bytes to fill dim floats in [-1, 1]
        bytes_needed = self.dim * 4  # 4 bytes per float32
        buf = bytearray()
        counter = 0
        seed = text.encode("utf-8")
        while len(buf) < bytes_needed:
            h = hashlib.md5(seed + counter.to_bytes(4, "little")).digest()
            buf.extend(h)
            counter += 1

        # Pack as uint32 (always finite, non-NaN) then map to [-1, 1)
        uints = struct.unpack(f"<{self.dim}I", bytes(buf[:bytes_needed]))
        scale = 2.0 / (2**32)
        vector = tuple((u * scale) - 1.0 for u in uints)

        # L2 normalize (so cosine = dot product)
        norm = math.sqrt(sum(v * v for v in vector))
        if norm < 1e-12:
            # Degenerate case — return zero vector
            return tuple(0.0 for _ in range(self.dim))
        return tuple(v / norm for v in vector)
