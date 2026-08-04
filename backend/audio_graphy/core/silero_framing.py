"""Silero VAD framing constants, shared by the streaming and batch paths.

These live in a module with NO imports on purpose. Both consumers run Silero
over the same waveform and must frame it identically — a batch run and a
streaming run of one recording that disagreed on window size would produce
different segment boundaries depending on which door the audio came in, and
nobody would notice until two runs of the same file diverged.

The obvious place for them was ``adapters.real.streaming_vad_silero``, and the
batch service originally imported them from there. That is what broke its
container: importing anything under ``audio_graphy.adapters.real`` executes
``adapters/__init__.py``, which imports ``adapters.bundle``, which reaches the
LLM, embedding and ASR adapters. The batch VAD image would have had to ship the
whole adapter tree to read three integers — and the Dockerfile that did not
copy it failed at import, after the build succeeded.

Layering: ``adapters.real.streaming_vad_silero`` imports this, which is upward
and frozen in ``tests/architecture/test_layering.py``. It belongs to that file's
accepted group -- a dependency-free leaf whose only sin is where it lives. This
module imports nothing at all (asserted by the batch service's COPY-closure
test), so the edge cannot close a cycle. ``core`` is where it sits because the
alternative is the adapter tree the paragraph above describes.

L3 locked: changing any of these changes segmentation for every deployment.
"""

from __future__ import annotations

#: Silero's ONNX export is trained at this rate; ffmpeg resamples to it.
SILERO_SAMPLE_RATE: int = 16000

#: Samples per inference window. The exported graph fixes this — 512 at 16 kHz.
SILERO_CHUNK_SAMPLES: int = 512

#: int16, so two bytes per sample.
SILERO_CHUNK_BYTES: int = SILERO_CHUNK_SAMPLES * 2

#: 0.032s. The resolution of every boundary either path can report.
SILERO_CHUNK_SEC: float = SILERO_CHUNK_SAMPLES / SILERO_SAMPLE_RATE

__all__ = [
    "SILERO_CHUNK_BYTES",
    "SILERO_CHUNK_SAMPLES",
    "SILERO_CHUNK_SEC",
    "SILERO_SAMPLE_RATE",
]
