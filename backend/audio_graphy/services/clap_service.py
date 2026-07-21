"""audiography-clap-service — FastAPI wrapper around laion_clap HTSAT-base.

Endpoints (M7 architecture §6.1):

    POST /v1/audio/embed    — multipart audio → 512-d CLAP embedding
    GET  /health            — liveness

GPU is mandatory at startup (L8 locked): if ``torch.cuda.is_available()``
is False, the service exits with code 1.

Standalone server — runs in its own Docker container (``clap-service``).
Started via::

    uvicorn audio_graphy.services.clap_service:app --host 0.0.0.0 --port 8006

Caching:
    A simple LRU on ``sha256(audio_bytes)`` → embedding (size 256) avoids
    re-encoding when the same file is POSTed twice (e.g. segment-level
    extract after a full-file extract).

Resampling:
    laion_clap requires 48 kHz mono input. ``librosa.load(path, sr=48000,
    mono=True)`` handles arbitrary input formats / sample rates.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_MODEL_NAME = "clap-htsat-base-2022"
_EXPECTED_DIM = 512  # L1 locked
_TARGET_SR = 48000    # laion_clap 强制
_CACHE_SIZE = 256

# Module-level state populated in lifespan.
_CLAP_MODEL: Any = None
_CACHE: dict[str, list[float]] = {}
_CACHE_ORDER: list[str] = []


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 normalize a 1-D vector; safe for zero-norm input."""
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return vec
    return vec / norm


def _cache_get(key: str) -> list[float] | None:
    """LRRU-style get: bump key to the back of the order list on hit."""
    if key not in _CACHE:
        return None
    _CACHE_ORDER.remove(key)
    _CACHE_ORDER.append(key)
    return _CACHE[key]


def _cache_put(key: str, value: list[float]) -> None:
    """Insert / update cache entry, evicting oldest when over size."""
    if key in _CACHE:
        _CACHE_ORDER.remove(key)
    _CACHE[key] = value
    _CACHE_ORDER.append(key)
    while len(_CACHE_ORDER) > _CACHE_SIZE:
        oldest = _CACHE_ORDER.pop(0)
        _CACHE.pop(oldest, None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load CLAP model at startup; enforce GPU availability (L8)."""
    global _CLAP_MODEL, _CACHE_SIZE
    try:
        import torch

        if not torch.cuda.is_available():
            logger.error(
                "clap-service requires CUDA (L8 locked); exiting. "
                "Set CUDA_VISIBLE_DEVICES and rerun."
            )
            sys.exit(1)
    except ImportError:
        logger.error("torch not installed; clap-service cannot start.")
        sys.exit(1)

    try:
        from laion_clap import CLAP_Module

        _CLAP_MODEL = CLAP_Module(enable_fusion=False, amodel="HTSAT-base")
        _CLAP_MODEL.load_ckpt()  # auto-downloads CLAP_weights_2022.pth
        logger.info("CLAP model loaded (cuda, amodel=HTSAT-base)")
    except Exception:
        logger.exception("Failed to load CLAP model; exiting.")
        sys.exit(1)

    yield

    _CLAP_MODEL = None
    logger.info("CLAP model released")


app = FastAPI(
    title="audiography-clap-service",
    version="1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + model load status."""
    import torch

    return {
        "status": "ok",
        "gpu": torch.cuda.is_available(),
        "model_loaded": _CLAP_MODEL is not None,
        "model": _MODEL_NAME,
        "cache_size": len(_CACHE),
    }


@app.post("/v1/audio/embed")
async def embed_audio(
    audio: UploadFile = File(...),
    model: str | None = None,
) -> JSONResponse:
    """Return 512-d CLAP embedding for the uploaded audio.

    Cache key = ``sha256(audio_bytes)``. Model param ignored for now
    (single model); accepted for forward-compatibility.
    """
    del model  # reserved
    if _CLAP_MODEL is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLAP model not loaded",
        )

    raw_bytes = await audio.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )

    key = hashlib.sha256(raw_bytes).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return JSONResponse(
            {
                "embedding": cached,
                "dim": _EXPECTED_DIM,
                "model": _MODEL_NAME,
                "duration_sec": 0.0,
                "cached": True,
            }
        )

    # Persist to tmp file so librosa can load any format.
    import tempfile

    import librosa

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        try:
            y, sr = librosa.load(tmp.name, sr=_TARGET_SR, mono=True)
        except Exception as exc:
            logger.warning("librosa.load failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported audio format: {exc}",
            ) from exc

        if y.size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="empty audio after decode",
            )

        duration_sec = float(len(y) / sr)

        # laion_clap expects shape (n_samples,) float32; get_audio_embedding_from_filedata
        # accepts a list of paths OR a numpy array when use_signal=True.
        try:
            vec = _CLAP_MODEL.get_audio_embedding_from_filedata(
                x=y.reshape(1, -1), use_tensor=False
            )
        except Exception as exc:
            logger.exception("CLAP inference failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CLAP inference failed",
            ) from exc

    vec_np = np.asarray(vec, dtype=np.float32).flatten()
    if vec_np.shape[0] != _EXPECTED_DIM:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"CLAP dim mismatch: got {vec_np.shape[0]}, expected {_EXPECTED_DIM}",
        )
    vec_np = _l2_normalize(vec_np)
    out_list = vec_np.tolist()

    _cache_put(key, out_list)

    return JSONResponse(
        {
            "embedding": out_list,
            "dim": _EXPECTED_DIM,
            "model": _MODEL_NAME,
            "duration_sec": duration_sec,
            "cached": False,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8006)
