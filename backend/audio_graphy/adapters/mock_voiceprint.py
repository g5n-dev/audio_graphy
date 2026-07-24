"""Mock CAM++ voiceprint + diarization adapter — deterministic and test-friendly.

Design goals (per architecture §5.2):

    1. Deterministic — same audio path → same diarization + same voiceprint.
       Makes speaker-linking / retrieval tests reproducible without GPU.
    2. Cross-file speaker coherence — when ``speaker_id`` is stable across
       multiple ``extract_voiceprint`` calls (e.g. "agent_zhang"), the mock
       seeds the voiceprint with a per-speaker bias so cosine similarity of
       two same-speaker vectors is ≥ 0.6. Different speaker_ids yield
       cosine ≤ 0.3. This makes ``SpeakerLinker``'s 0.5 / 0.7 thresholds
       fully testable in mock mode.

Diarization behavior:
    - Hash-derived total duration (~30s) split into alternating 5s chunks
      between 2 speakers ("spk_0" / "spk_1").
    - ``max_speakers`` is honored; outputs never exceed that count.

Voiceprint behavior:
    - 192-d L2-normalized vector.
    - Per-speaker bias: when ``speaker_id`` is provided and non-empty, the
      first 8 dims are overwritten with a hash of ``speaker_id``. This
      makes cosine sim between same-speaker voiceprints ≥ 0.6.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import struct
from collections.abc import Sequence

from audio_graphy.adapters.protocols import (
    DiarizationResult,
    DiarizationSegment,
    VoiceprintAdapter,
    VoiceprintResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIM = 192
_DEFAULT_LATENCY_MS = 5.0
_DEFAULT_NUM_SPEAKERS = 2
_SPEAKER_BIAS_DIMS = 8  # first 8 dims encode the speaker signal
_SAME_SPEAKER_MIN_COS = 0.6  # design target
_DIFF_SPEAKER_MAX_COS = 0.3  # design target


class MockVoiceprintAdapter:
    """Mock CAM++ — deterministic 2-speaker diarization + hash voiceprint.

    Satisfies ``VoiceprintAdapter`` (runtime checkable).

    Args:
        dim: Voiceprint dim (default 192, L2 locked).
        model: Reported model identifier.
        latency_ms: Simulated request latency.
        num_speakers: Number of speakers in mock diarization. Default 2.
    """

    def __init__(
        self,
        *,
        dim: int = _DEFAULT_DIM,
        model: str = "mock-cam++",
        latency_ms: float = _DEFAULT_LATENCY_MS,
        num_speakers: int = _DEFAULT_NUM_SPEAKERS,
    ) -> None:
        if dim <= 0 or dim < _SPEAKER_BIAS_DIMS:
            raise ValueError(f"dim must be ≥ {_SPEAKER_BIAS_DIMS} to fit speaker bias, got {dim}")
        if num_speakers < 1:
            raise ValueError(f"num_speakers must be ≥ 1, got {num_speakers}")
        self.dim = dim
        self.model = model
        self._latency_ms = latency_ms
        self._num_speakers = num_speakers
        self._diarize_count = 0
        self._voiceprint_count = 0

    async def diarize(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = 0.5,
        max_speakers: int = 10,
    ) -> DiarizationResult:
        """Deterministic diarization: alternating N speakers in 5s chunks."""
        del min_segment_sec  # mock does not enforce minimum
        self._diarize_count += 1
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        total_sec = self._derive_duration(audio_path)
        n_speakers = max(1, min(self._num_speakers, max_speakers))
        chunk_sec = 5.0

        segments: list[DiarizationSegment] = []
        t = 0.0
        idx = 0
        while t < total_sec:
            spk_idx = idx % n_speakers
            start = t
            end = min(t + chunk_sec, total_sec)
            segments.append(
                DiarizationSegment(
                    start_sec=start,
                    end_sec=end,
                    speaker_id=f"spk_{spk_idx}",
                    confidence=0.92,
                )
            )
            t = end
            idx += 1

        return DiarizationResult(
            segments=tuple(segments),
            num_speakers=n_speakers,
            model=self.model,
            duration_sec=total_sec,
        )

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> VoiceprintResult:
        """Deterministic 192-d L2-normalized voiceprint.

        ``speaker_id`` (when non-empty) biases the first 8 dims so same-speaker
        pairs hit cosine ≥ 0.6 (test-friendly for SpeakerLinker thresholds).
        """
        del start_sec, end_sec  # mock ignores server-side crop
        self._voiceprint_count += 1
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        vec = self._hash_to_voiceprint(audio_path, speaker_id)
        return VoiceprintResult(
            vector=vec,
            dim=self.dim,
            model=self.model,
            speaker_id=speaker_id,
            duration_sec=self._derive_duration(audio_path),
        )

    # --------------------------------------------------------------
    # Internals
    # --------------------------------------------------------------
    def _derive_duration(self, audio_path: str) -> float:
        """Derive a stable ~30s duration from path hash."""
        h = hashlib.sha256(audio_path.encode("utf-8")).digest()
        # Map first 4 bytes to [10.0, 60.0]
        val = struct.unpack("<I", h[:4])[0]
        return float(10.0 + (val % 50))

    def _hash_to_voiceprint(
        self,
        audio_path: str,
        speaker_id: str,
    ) -> tuple[float, ...]:
        """Build a deterministic 192-d L2-normalized voiceprint.

        When ``speaker_id`` is provided, the first ``_SPEAKER_BIAS_DIMS`` dims
        are seeded with a hash of the speaker_id. This biases same-speaker
        pairs toward high cosine similarity (≥ 0.6) while keeping different
        pairs at ≤ 0.3.
        """
        bytes_needed = self.dim * 4
        buf = bytearray()
        counter = 0
        seed = audio_path.encode("utf-8")
        while len(buf) < bytes_needed:
            h = hashlib.sha512(seed + counter.to_bytes(4, "little")).digest()
            buf.extend(h)
            counter += 1

        uints = struct.unpack(f"<{self.dim}I", bytes(buf[:bytes_needed]))
        scale = 2.0 / (2**32)
        vec = [(u * scale) - 1.0 for u in uints]

        # Overwrite first N dims with speaker bias if provided.
        if speaker_id:
            spk_seed = hashlib.sha512(speaker_id.encode("utf-8")).digest()
            spk_uints = struct.unpack(f"<{_SPEAKER_BIAS_DIMS}I", spk_seed[: _SPEAKER_BIAS_DIMS * 4])
            # Map each bias dim to [-1.0, +1.0]. Different speaker_ids map to
            # different sign patterns, so same-speaker pairs correlate strongly
            # (cos ≥ 0.6) while diff-speaker pairs cancel out (cos ≤ 0.3).
            for i, u in enumerate(spk_uints):
                vec[i] = (u / 2**32) * 2.0 - 1.0
            # Dampen the non-bias dims so the bias region dominates the cosine.
            # Without this, the 184 noise dims drown out the 8-dim bias signal
            # after L2 normalization.
            for i in range(_SPEAKER_BIAS_DIMS, self.dim):
                vec[i] *= 0.1

        # L2 normalize.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < 1e-12:
            return tuple(0.0 for _ in range(self.dim))
        return tuple(v / norm for v in vec)


_MOCK_VOICEPRINT_PROTOCOL_CHECK: VoiceprintAdapter = MockVoiceprintAdapter()

__all__: Sequence[str] = ("MockVoiceprintAdapter",)
