"""audiography-campplus-service — FastAPI wrapper around funasr CAM++.

Endpoints (M7 architecture §6.2):

    POST /v1/diarize             — full audio → speaker-segmented timeline
    POST /v1/voiceprint/extract  — audio → 192-d L2-normalized voiceprint
    GET  /health                 — liveness

CPU-default, GPU-optional (L8). Set ``CAMPPLUS_DEVICE=cuda`` to enable GPU.

Model: ``iic/speech_campplus_sv_zh-cn_16k-common`` (192-d, L2 locked).
       Diarization uses ``iic/speech_eres2svjavan ZoomCaption`` when available;
       this service wraps the SV (speaker verification) model with a simple
       energy-based VAD for diarization. Production deployments may swap in
       ``iic/speech_cam++_sv`` for richer clustering.

Standalone server — runs in its own Docker container (``campplus-service``).
Started via::

    uvicorn audio_graphy.services.campplus_service:app --host 0.0.0.0 --port 8007
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_SV_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"
_DIARIZE_MODEL_ID = "iic/speech_eres2svjavan_zoom_caption_zh-cn_16k-common"
_TARGET_SR = 16000
_EXPECTED_DIM = 192  # L2 locked

# Module-level state populated in lifespan.
_SV_MODEL: Any = None  # speaker-verification CAM++ model
_DIARIZE_MODEL: Any = None  # diarization (ERO2SV) model; optional
_DEVICE: str = "cpu"

# Inference runs off the event loop so /health and queued requests stay
# responsive, but it must stay strictly serial: concurrent forward passes
# would exhaust GPU memory.
_INFERENCE_SEMAPHORE = asyncio.Semaphore(1)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Preload CAM++ SV model at startup. Diarize model loads lazily."""
    global _SV_MODEL, _DIARIZE_MODEL, _DEVICE
    try:
        from funasr import AutoModel

        _DEVICE = os.environ.get("CAMPPLUS_DEVICE", "cpu").lower()
        logger.info("Loading CAM++ SV model %s on device=%s", _SV_MODEL_ID, _DEVICE)
        _SV_MODEL = AutoModel(model=_SV_MODEL_ID, device=_DEVICE)
        logger.info("CAM++ SV model loaded")
    except Exception:
        logger.exception("Failed to load CAM++ SV model at startup; exiting.")
        sys.exit(1)

    # Diarize model is optional — defer until first call if missing.
    try:
        from funasr import AutoModel

        _DIARIZE_MODEL = AutoModel(
            model=_DIARIZE_MODEL_ID,
            device=_DEVICE,
            disable_update=True,
        )
        logger.info("CAM++ diarization model loaded")
    except Exception:
        logger.warning(
            "Diarization model %s unavailable; /v1/diarize will fallback "
            "to SV-based single-speaker diarization.",
            _DIARIZE_MODEL_ID,
        )
        _DIARIZE_MODEL = None

    yield

    _SV_MODEL = None
    _DIARIZE_MODEL = None
    logger.info("CAM++ models released")


app = FastAPI(
    title="audiography-campplus-service",
    version="1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + model load status."""
    return {
        "status": "ok",
        "device": _DEVICE,
        "sv_loaded": _SV_MODEL is not None,
        "diarize_loaded": _DIARIZE_MODEL is not None,
        "sv_model": _SV_MODEL_ID,
        "dim": _EXPECTED_DIM,
    }


def _save_tmp(raw_bytes: bytes, suffix: str) -> str:
    """Write bytes to a NamedTemporaryFile; return path. Caller cleans up."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        return tmp.name


def _unlink_tmp(path: str) -> None:
    """Best-effort temporary audio cleanup with residue observability."""
    try:
        os.unlink(path)
    except OSError as exc:
        logger.warning("Temporary audio cleanup failed path=%s: %s", path, exc)


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 normalize a 1-D vector; safe for zero-norm input."""
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return vec
    return vec / norm


@app.post("/v1/diarize")
async def diarize(
    audio: UploadFile = File(...),
    min_segment_sec: float = Form(0.5),
    max_speakers: int = Form(10),
) -> JSONResponse:
    """Run CAM++ diarization → speaker-segmented timeline."""
    if _SV_MODEL is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CAM++ SV model not loaded",
        )

    raw_bytes = await audio.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    tmp_path = _save_tmp(raw_bytes, suffix)
    # Bound once, on the loop: shutdown may clear the global while the worker
    # thread is running, and the branch below must not disagree with what the
    # thread ends up using.
    diarize_model = _DIARIZE_MODEL

    # asyncio.to_thread cannot interrupt a running thread: on cancellation the
    # await returns while the worker keeps reading the file. So the worker owns
    # cleanup once it starts, and the caller only cleans up when it never did.
    worker_started = False

    def _run() -> tuple[list[dict[str, Any]], float]:
        nonlocal worker_started
        worker_started = True
        try:
            if diarize_model is not None:
                return _diarize_with_diarize_model(
                    diarize_model, tmp_path, min_segment_sec, max_speakers
                )
            # Fallback: SV-only — single-speaker timeline (whole file).
            return _diarize_with_sv_only(tmp_path, min_segment_sec)
        finally:
            _unlink_tmp(tmp_path)

    try:
        async with _INFERENCE_SEMAPHORE:
            segments, duration = await asyncio.to_thread(_run)
    except BaseException:
        if not worker_started:
            _unlink_tmp(tmp_path)
        raise

    num_speakers = len({s["speaker_id"] for s in segments})
    return JSONResponse(
        {
            "segments": segments,
            "num_speakers": num_speakers,
            "model": _SV_MODEL_ID,
            "duration_sec": duration,
        }
    )


@app.post("/v1/voiceprint/extract")
async def extract_voiceprint(
    audio: UploadFile = File(...),
    speaker_id: str = Form(""),
    start_sec: float | None = Form(None),
    end_sec: float | None = Form(None),
) -> JSONResponse:
    """Extract 192-d L2-normalized voiceprint for the (cropped) audio."""
    if _SV_MODEL is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CAM++ SV model not loaded",
        )

    raw_bytes = await audio.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    tmp_path = _save_tmp(raw_bytes, suffix)
    try:
        # Optional server-side crop.
        if start_sec is not None or end_sec is not None:
            tmp_path = _crop_audio(
                tmp_path,
                start_sec if start_sec is not None else 0.0,
                end_sec if end_sec is not None else None,
            )

        try:
            async with _INFERENCE_SEMAPHORE:
                result = await asyncio.to_thread(
                    _SV_MODEL.generate,
                    input=tmp_path,
                    cache={},
                    language="zh-cn",
                )
        except Exception as exc:
            logger.exception("CAM++ SV inference failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"CAM++ SV inference failed: {exc}",
            ) from exc

        # funasr returns list of dicts; spk_embedding is the canonical key.
        if not result or not isinstance(result, list):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CAM++ SV returned no result",
            )
        item = result[0]
        spk_vec = item.get("spk_embedding") if isinstance(item, dict) else None
        if spk_vec is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CAM++ SV result missing 'spk_embedding'",
            )

        vec_np = np.asarray(spk_vec, dtype=np.float32).flatten()
        if vec_np.shape[0] != _EXPECTED_DIM:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"CAM++ dim mismatch: got {vec_np.shape[0]}, expected {_EXPECTED_DIM}",
            )
        vec_np = _l2_normalize(vec_np)
    finally:
        _unlink_tmp(tmp_path)

    return JSONResponse(
        {
            "voiceprint": vec_np.tolist(),
            "dim": _EXPECTED_DIM,
            "model": _SV_MODEL_ID,
            "speaker_id": speaker_id,
            "duration_sec": 0.0,
        }
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _diarize_with_diarize_model(
    model: Any,
    path: str,
    min_segment_sec: float,
    max_speakers: int,
) -> tuple[list[dict[str, Any]], float]:
    """Invoke the dedicated diarization model and parse its output.

    funasr Eres2SV-style models return ``sentence_info`` entries with
    ``spk_label`` (0-based). We convert to the API schema.

    The model is passed in rather than read from the module global: this runs on
    a worker thread, and lifespan shutdown clears that global, so re-reading it
    here could observe ``None`` mid-inference.
    """
    res = model.generate(
        input=path,
        cache={},
        max_spk_num=max_speakers,
    )
    if not res or not isinstance(res, list):
        return [], 0.0

    item = res[0]
    sentence_info = item.get("sentence_info") if isinstance(item, dict) else None
    if not sentence_info:
        return [], 0.0

    import librosa  # lazy

    y, sr = librosa.load(path, sr=_TARGET_SR, mono=True)
    duration = float(len(y) / sr) if sr > 0 else 0.0

    segments: list[dict[str, Any]] = []
    for s in sentence_info:
        if not isinstance(s, dict):
            continue
        start = float(s.get("start", 0.0))
        end = float(s.get("end", start + min_segment_sec))
        if end - start < min_segment_sec:
            continue
        spk = s.get("spk_label", s.get("spk", 0))
        segments.append(
            {
                "start_sec": start,
                "end_sec": end,
                "speaker_id": f"spk_{int(spk or 0)}",
                "confidence": float(s.get("confidence", 1.0)),
            }
        )
    return segments, duration


def _diarize_with_sv_only(
    path: str,
    min_segment_sec: float,
) -> tuple[list[dict[str, Any]], float]:
    """Fallback: whole-file single-speaker diarization via librosa duration."""
    import librosa  # lazy

    y, sr = librosa.load(path, sr=_TARGET_SR, mono=True)
    duration = float(len(y) / sr) if sr > 0 else 0.0
    if duration < min_segment_sec:
        return [], duration
    return [
        {
            "start_sec": 0.0,
            "end_sec": duration,
            "speaker_id": "spk_0",
            "confidence": 1.0,
        }
    ], duration


def _crop_audio(path: str, start_sec: float, end_sec: float | None) -> str:
    """Server-side crop using librosa; write to a new tmp file."""
    import librosa
    import soundfile as sf

    y, sr = librosa.load(
        path,
        sr=_TARGET_SR,
        mono=True,
        offset=start_sec,
        duration=(end_sec - start_sec) if end_sec is not None else None,
    )
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as new_tmp:
        new_tmp_path = new_tmp.name
    sf.write(new_tmp_path, y, sr)
    _unlink_tmp(path)
    return new_tmp_path


if __name__ == "__main__":
    import uvicorn

    # Container entry point must listen beyond loopback.
    uvicorn.run(app, host="0.0.0.0", port=8007)  # noqa: S104
