"""Batch Silero VAD as an HTTP service — the one stage compose had no container for.

Every other model in the pipeline ships as a container (funASR, BGE-M3, CAM++,
Ollama/vLLM). VAD did not, so ``models-cpu`` still left segmentation on the mock
adapter, which derives cut points from file size and has nothing to do with
speech. This closes that hole with the same shape as its siblings:
``docker/silero-vad-service/Dockerfile`` plus this app.

**The contract is fixed by the caller, not by this file.**
:mod:`audio_graphy.adapters.real.vad_silero` already speaks a specific protocol
and predates this service:

    POST /v1/vad/segment   multipart/form-data
      audio             the file part -- that exact name
      min_segment_sec   str(float)
      max_segment_sec   str(float)
    200 -> {"segments": [{"start_sec", "end_sec", "confidence"}], "model": str}

**Inference is shared with the streaming path, deliberately.**
:mod:`audio_graphy.adapters.real.streaming_vad_silero` already runs Silero over
512-sample windows at 16 kHz with the LSTM state threaded between them. Batch
differs only in having the whole file up front, so it imports that module's
constants and its ONNX feed/state convention rather than growing a second,
separately-tuned integration. Two integrations would mean the same recording
segments differently depending on which door it came in -- and nobody would
notice until two runs of one file disagreed.

The thresholds ARE deliberately independent of the streaming FSM's settings:
streaming trades latency against truncation and is tuned for that, while batch
sees the whole waveform. They are module constants here, and
:mod:`tests.services.test_silero_vad_service` pins the ones that must match
(sample rate, window size) against the streaming module so a change to either
side fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status

from audio_graphy.core.silero_framing import (
    SILERO_CHUNK_SAMPLES,
    SILERO_CHUNK_SEC,
    SILERO_SAMPLE_RATE,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "silero-vad-batch"

#: Probability at or above which a window counts as speech.
#:
#: Independent of the streaming FSM's onset/offset pair on purpose: streaming
#: opens early and closes late to avoid clipping a live utterance, while batch
#: sees the whole waveform and can afford one symmetric threshold. Silero's own
#: documented default.
SPEECH_THRESHOLD = 0.5

#: Speech separated by less than this much silence is one segment. Below it,
#: ordinary within-sentence pauses would shatter a single utterance into
#: fragments that ASR then transcribes without their context.
MIN_SILENCE_SEC = 0.35

#: Padding kept on both sides of a detected segment. Silero's probability rises
#: a window or two into the speech; without padding the first phoneme is cut.
SPEECH_PAD_SEC = 0.10

_MAX_UPLOAD_BYTES = int(os.getenv("SILERO_VAD_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
_DECODE_TIMEOUT_SEC = float(os.getenv("SILERO_VAD_DECODE_TIMEOUT_SEC", "300"))
#: Wall-clock ceiling on one inference. The adapter's own read timeout is 30s
#: (vad_silero._DEFAULT_TIMEOUT); anything past that is a client-side failure
#: with the server still holding the single concurrency slot. Failing fast here
#: turns "the next request queues behind a run nobody is waiting for" into an
#: honest 503.
_INFER_TIMEOUT_SEC = float(os.getenv("SILERO_VAD_INFER_TIMEOUT_SEC", "25"))
_MAX_CONCURRENCY = int(os.getenv("SILERO_VAD_MAX_CONCURRENCY", "1"))

_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENCY)
_session: Any = None
_load_error: str | None = None


@dataclass(frozen=True, slots=True)
class _Span:
    start_sec: float
    end_sec: float
    confidence: float


def _model_path() -> str:
    return os.getenv("SILERO_VAD_MODEL_PATH", "/models/silero_vad.onnx")


def _load_session() -> Any:
    """Create the ONNX session. Raises on failure; the caller maps it to 503."""

    import onnxruntime  # imported lazily: absent from the API image

    options = onnxruntime.SessionOptions()
    threads = int(os.getenv("SILERO_VAD_ONNX_THREADS", "1"))
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = threads
    return onnxruntime.InferenceSession(
        _model_path(),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the model at startup so the first caller does not pay for it."""

    global _session, _load_error
    try:
        _session = await asyncio.to_thread(_load_session)
        logger.info("Silero VAD model loaded from %s", _model_path())
    except Exception as exc:
        # Reported through /health and a 503 on the route rather than crashing the
        # container: an operator who has not supplied the model yet needs to see
        # WHY, and a crash-looping container says nothing.
        _load_error = str(exc)
        logger.error("Silero VAD model failed to load: %s", exc)
    yield
    _session = None


app = FastAPI(title="AudioGraphy Silero VAD", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok" if _session is not None else "degraded",
        "model": MODEL_NAME,
        "model_loaded": _session is not None,
        "error": _load_error,
    }


def _require_session() -> Any:
    if _session is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"silero model unavailable: {_load_error or 'not loaded'}",
        )
    return _session


async def _decode_to_pcm16(raw: bytes, suffix: str) -> bytes:
    """Decode any container ffmpeg understands into 16 kHz mono s16le."""

    if shutil.which("ffmpeg") is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ffmpeg is not available in this image",
        )
    with tempfile.NamedTemporaryFile(suffix=suffix or ".bin", delete=False) as handle:
        handle.write(raw)
        source = handle.name
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            source,
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            str(SILERO_SAMPLE_RATE),
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_DECODE_TIMEOUT_SEC
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="audio decode timed out",
            ) from None
        if process.returncode != 0 or not stdout:
            # A decode failure is the caller's malformed upload, not our fault.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"cannot decode audio: {stderr.decode('utf-8', 'replace')[:200]}",
            )
        return stdout
    finally:
        with contextlib.suppress(OSError):
            Path(source).unlink()


def probabilities(session: Any, pcm: bytes) -> list[float]:
    """Speech probability per 512-sample window, LSTM state carried across.

    Mirrors ``streaming_vad_silero._run_onnx``: same input-name tolerance, same
    (prob, h, c) output convention, same normalisation. A trailing partial
    window is zero-padded rather than dropped, so the tail of a recording is
    never silently discarded.
    """

    import numpy as np

    def _input_name(candidates: Sequence[str]) -> str:
        names = {meta.name for meta in session.get_inputs()}
        for candidate in candidates:
            if candidate in names:
                return candidate
        return str(session.get_inputs()[0].name)

    audio_name = _input_name(("input", "audio", "waveform"))
    h_name = _input_name(("h", "hn", "lstm_h"))
    c_name = _input_name(("c", "cn", "lstm_c"))

    state_h = np.zeros((2, 1, 64), dtype=np.float32)
    state_c = np.zeros((2, 1, 64), dtype=np.float32)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    out: list[float] = []
    for offset in range(0, len(samples), SILERO_CHUNK_SAMPLES):
        window = samples[offset : offset + SILERO_CHUNK_SAMPLES]
        if len(window) < SILERO_CHUNK_SAMPLES:
            window = np.pad(window, (0, SILERO_CHUNK_SAMPLES - len(window)))
        outputs = session.run(
            None,
            {audio_name: window.reshape(1, -1), h_name: state_h, c_name: state_c},
        )
        out.append(max(0.0, min(1.0, float(np.squeeze(outputs[0])))))
        if len(outputs) >= 3:
            state_h, state_c = outputs[1], outputs[2]
    return out


def spans_from_probabilities(
    probs: Sequence[float],
    *,
    min_segment_sec: float,
    max_segment_sec: float,
) -> list[_Span]:
    """Merge per-window probabilities into segments the chunker can use.

    Three rules, in order, each one a defect if omitted:

    * gaps shorter than ``MIN_SILENCE_SEC`` do not end a segment -- otherwise a
      breath mid-sentence splits one utterance into fragments ASR then
      transcribes without their context;
    * a segment shorter than ``min_segment_sec`` is dropped -- a 0.2s blip is
      noise, and embedding it wastes a provider call on nothing;
    * a segment longer than ``max_segment_sec`` is split at that boundary --
      the caller's ASR has a per-request ceiling, and a 20-minute monologue that
      never pauses would exceed it.
    """

    max_gap_windows = max(1, round(MIN_SILENCE_SEC / SILERO_CHUNK_SEC))
    raw: list[tuple[int, int]] = []
    start: int | None = None
    silence = 0

    for index, prob in enumerate(probs):
        if prob >= SPEECH_THRESHOLD:
            if start is None:
                start = index
            silence = 0
        elif start is not None:
            silence += 1
            if silence >= max_gap_windows:
                raw.append((start, index - silence + 1))
                start = None
                silence = 0
    if start is not None:
        raw.append((start, len(probs)))

    spans: list[_Span] = []
    for first, last in raw:
        begin = max(0.0, first * SILERO_CHUNK_SEC - SPEECH_PAD_SEC)
        finish = min(len(probs) * SILERO_CHUNK_SEC, last * SILERO_CHUNK_SEC + SPEECH_PAD_SEC)
        window = probs[first:last] or [0.0]
        confidence = sum(window) / len(window)
        cursor = begin
        while finish - cursor > max_segment_sec:
            spans.append(_Span(cursor, cursor + max_segment_sec, confidence))
            cursor += max_segment_sec
        if finish - cursor >= min_segment_sec:
            spans.append(_Span(cursor, finish, confidence))
    return spans


@app.post("/v1/vad/segment")
async def segment(
    audio: UploadFile = File(...),
    min_segment_sec: float = Form(0.5),
    max_segment_sec: float = Form(30.0),
) -> dict[str, Any]:
    """Segment one recording. Field names are the adapter's, not ours to choose."""

    session = _require_session()
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty audio upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"audio exceeds {_MAX_UPLOAD_BYTES} bytes",
        )
    if min_segment_sec <= 0 or max_segment_sec <= min_segment_sec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="require 0 < min_segment_sec < max_segment_sec",
        )

    suffix = Path(audio.filename or "upload.wav").suffix
    pcm = await _decode_to_pcm16(raw, suffix)

    async with _SEMAPHORE:
        try:
            probs = await asyncio.wait_for(
                asyncio.to_thread(probabilities, session, pcm),
                timeout=_INFER_TIMEOUT_SEC,
            )
        except TimeoutError:
            # The adapter has already given up (its read timeout is shorter).
            # Returning frees the slot instead of holding it for a dead client.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"inference exceeded {_INFER_TIMEOUT_SEC}s; raise "
                    "SILERO_VAD_INFER_TIMEOUT_SEC and the caller's timeout together, "
                    "or split the recording"
                ),
            ) from None

    spans = spans_from_probabilities(
        probs, min_segment_sec=min_segment_sec, max_segment_sec=max_segment_sec
    )
    return {
        "segments": [
            {
                "start_sec": round(span.start_sec, 3),
                "end_sec": round(span.end_sec, 3),
                "confidence": round(span.confidence, 4),
            }
            for span in spans
        ],
        "model": MODEL_NAME,
        "duration_sec": round(len(probs) * SILERO_CHUNK_SEC, 3),
    }
