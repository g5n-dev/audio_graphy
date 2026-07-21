"""Mock CLAP audio embedding adapter — deterministic hash-based vectors.

Strategy (mirror mock_embed.py):
    - Same audio path → same 512-dim vector (deterministic via SHA-512).
    - Output is L2-normalized so cosine == dot product.
    - Optional simulated latency (default 5ms).

Used in CI / unit tests where the real CLAP service is unavailable. The
output schema matches ``AudioEmbeddingResult`` so downstream callers
(retrieval / speaker_linker / eval) can exercise the full data flow
without GPU.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import struct
from collections.abc import Sequence

from audio_graphy.adapters.protocols import AudioEmbedAdapter, AudioEmbeddingResult

logger = logging.getLogger(__name__)

_DEFAULT_DIM = 512
_DEFAULT_LATENCY_MS = 5.0


class MockAudioEmbedAdapter:
    """Deterministic mock CLAP — SHA-512(path) → 512-d L2-normalized vector.

    ``MockAudioEmbedAdapter`` satisfies ``AudioEmbedAdapter`` (runtime
    checkable). Two calls on the same path return identical vectors,
    which makes retrieval / cache tests reproducible.
    """

    def __init__(
        self,
        *,
        dim: int = _DEFAULT_DIM,
        model: str = "mock-clap-htsat",
        latency_ms: float = _DEFAULT_LATENCY_MS,
    ) -> None:
        if dim <= 0 or dim % 8 != 0:
            raise ValueError(f"dim must be positive multiple of 8, got {dim}")
        self.dim = dim
        self.model = model
        self._latency_ms = latency_ms
        self._call_count = 0

    async def embed_audio(
        self,
        audio_paths: Sequence[str],
        *,
        segment_ids: Sequence[int | None] | None = None,
    ) -> Sequence[AudioEmbeddingResult]:
        self._call_count += 1
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        ids: list[int | None]
        if segment_ids is None:
            ids = [None] * len(audio_paths)
        else:
            if len(segment_ids) != len(audio_paths):
                raise ValueError(
                    "segment_ids length must match audio_paths length "
                    f"({len(segment_ids)} != {len(audio_paths)})"
                )
            ids = list(segment_ids)

        results: list[AudioEmbeddingResult] = []
        for path, seg_id in zip(audio_paths, ids, strict=True):
            vec = self._hash_to_vector(path)
            results.append(
                AudioEmbeddingResult(
                    vector=vec,
                    dim=self.dim,
                    model=self.model,
                    segment_id=seg_id,
                    duration_sec=0.0,
                )
            )

        logger.debug(
            "Mock CLAP call #%d: %d paths × %d dim",
            self._call_count,
            len(audio_paths),
            self.dim,
        )
        return results

    def _hash_to_vector(self, path: str) -> tuple[float, ...]:
        """Deterministic dim-d vector from SHA-512(path).

        Uses repeated SHA-512 to fill ``dim`` float32 entries, then maps
        them to [-1, 1) and L2-normalizes.
        """
        bytes_needed = self.dim * 4
        buf = bytearray()
        counter = 0
        seed = path.encode("utf-8")
        while len(buf) < bytes_needed:
            h = hashlib.sha512(seed + counter.to_bytes(4, "little")).digest()
            buf.extend(h)
            counter += 1

        uints = struct.unpack(f"<{self.dim}I", bytes(buf[:bytes_needed]))
        scale = 2.0 / (2**32)
        vec = tuple((u * scale) - 1.0 for u in uints)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm < 1e-12:
            return tuple(0.0 for _ in range(self.dim))
        return tuple(v / norm for v in vec)


_MOCK_CLAP_PROTOCOL_CHECK: AudioEmbedAdapter = MockAudioEmbedAdapter()

__all__: Sequence[str] = ("MockAudioEmbedAdapter",)
