"""audiography-paraformer-service — OpenAI-compatible ASR over funasr Paraformer.

Replaces the ``funasr/server`` image, which does not exist on Docker Hub
(``/v2/repositories/funasr/server/`` → 404, no tags, no arm64 variant), so it
could never be pulled on any architecture. This service reimplements the exact
HTTP contract that ``audio_graphy.adapters.real.funasr.FunASRAdapter`` speaks,
using the funasr Python package directly — the same approach as
``campplus_service`` and native on arm64.

Endpoints:

    POST /v1/audio/transcriptions  — multipart audio → verbose_json transcript
    GET  /v1/models                — OpenAI-style model list (compose healthcheck)
    GET  /health                   — liveness + model load status

Request contract (multipart/form-data), per docs/m5-prd.md §4:
    file, model, language, response_format, temperature,
    timestamp_granularities[]

Response 200 (``response_format=verbose_json``)::

    {
        "text": str,
        "segments": [
            {"id": int, "start": float, "end": float, "text": str, "confidence": float},
            ...,
        ],
        "language": str,
        "duration": float,
        "model": str,
    }

Models — this is funasr's own ``paraformer-zh`` recipe, spelled out as explicit
ModelScope ids rather than aliases so a registry change cannot silently swap the
model under us. All three verified present (HTTP 200 on
``https://modelscope.cn/api/v1/models/<id>``) and confirmed to be what
``funasr.download.name_maps_from_hub.name_maps_ms`` resolves the aliases to:
    ASR  iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch
    VAD  iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
    PUNC iic/punc_ct-transformer_cn-en-common-vocab471067-large

SeACo-Paraformer specifically, not plain ``speech_paraformer-large_asr_nat-*``:
funasr only predicts token timestamps for the SeACo and vad-punc variants (see
``auto_model.py``, the "can predict timestamp" error), and without timestamps
there is no sentence segmentation — the response would degrade to one segment
spanning the whole file.

Started via::

    uvicorn audio_graphy.services.paraformer_service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)

_ASR_MODEL_ID = os.environ.get(
    "PARAFORMER_ASR_MODEL",
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
)
_VAD_MODEL_ID = os.environ.get(
    "PARAFORMER_VAD_MODEL",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
)
_PUNC_MODEL_ID = os.environ.get(
    "PARAFORMER_PUNC_MODEL",
    "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
)

# The served-model name the adapter sends (FUNASR_MODEL, default
# "fun-asr-nano"). It is a label, not a selector: this service hosts exactly
# one pipeline. Requests naming any other model are still served, and the
# response echoes the served name, so an existing .env keeps working.
_SERVED_MODEL_NAME = os.environ.get("FUNASR_MODEL", "fun-asr-nano")

_TARGET_SR = 16000
_DEFAULT_LANGUAGE = os.environ.get("FUNASR_LANGUAGE", "zh")

# funasr sentence timestamps are milliseconds.
_MS_PER_SEC = 1000.0

_MODEL: Any = None
_DEVICE: str = "cpu"

# Inference must stay strictly serial: funasr AutoModel is not re-entrant and
# concurrent forward passes on CPU only thrash the machine.
_INFERENCE_SEMAPHORE = asyncio.Semaphore(1)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Preload the Paraformer + VAD + PUNC pipeline at startup."""
    global _MODEL, _DEVICE
    try:
        from funasr import AutoModel

        _DEVICE = os.environ.get("FUNASR_DEVICE", "cpu").lower()
        logger.info(
            "Loading Paraformer pipeline asr=%s vad=%s punc=%s device=%s",
            _ASR_MODEL_ID,
            _VAD_MODEL_ID,
            _PUNC_MODEL_ID or "(disabled)",
            _DEVICE,
        )
        kwargs: dict[str, Any] = {
            "model": _ASR_MODEL_ID,
            "vad_model": _VAD_MODEL_ID,
            "device": _DEVICE,
            "disable_update": True,
        }
        # Punctuation is optional but costly to skip. It is the largest download
        # (~1.1 GB against 990 MB for the ASR model), so a bandwidth- or
        # disk-constrained deployment can drop it by setting PARAFORMER_PUNC_MODEL
        # empty — accepting a real downgrade, not just missing commas.
        #
        # Measured with it disabled: funasr logs "punc_model is required for
        # sentence_timestamp, skipping sentence segmentation" and returns no
        # sentence_info at all, so a 16.5s multi-sentence recording came back as
        # one segment spanning the whole file. Transcription accuracy is
        # unaffected (character-exact in both cases); what is lost is the
        # sentence timeline the rest of the pipeline aligns speakers against.
        if _PUNC_MODEL_ID:
            kwargs["punc_model"] = _PUNC_MODEL_ID
        _MODEL = AutoModel(**kwargs)
        logger.info("Paraformer pipeline loaded")
    except Exception:
        # Failing loudly beats serving 503s forever: compose restarts the
        # container and the failure is visible in `docker compose logs`.
        logger.exception("Failed to load Paraformer pipeline at startup; exiting.")
        sys.exit(1)

    yield

    _MODEL = None
    logger.info("Paraformer pipeline released")


app = FastAPI(
    title="audiography-paraformer-service",
    version="1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + model load status."""
    return {
        "status": "ok" if _MODEL is not None else "loading",
        "device": _DEVICE,
        "model_loaded": _MODEL is not None,
        "asr_model": _ASR_MODEL_ID,
        "vad_model": _VAD_MODEL_ID,
        "punc_model": _PUNC_MODEL_ID,
        "served_model_name": _SERVED_MODEL_NAME,
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """OpenAI-style model list. Also the compose healthcheck target."""
    return {
        "object": "list",
        "data": [
            {
                "id": _SERVED_MODEL_NAME,
                "object": "model",
                "owned_by": "audiography",
                "root": _ASR_MODEL_ID,
            }
        ],
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


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(_SERVED_MODEL_NAME),
    language: str = Form(_DEFAULT_LANGUAGE),
    response_format: str = Form("verbose_json"),
    temperature: float = Form(0.0),
    # Annotated as the Response base class, not `JSONResponse | PlainTextResponse`:
    # FastAPI derives a response model from the return annotation and rejects a
    # union of Response subclasses outright, which crashes at import time.
) -> Response:
    """Transcribe one audio file. OpenAI ``/v1/audio/transcriptions`` shape.

    ``temperature`` is accepted for wire compatibility and ignored: Paraformer
    is a non-autoregressive CTC-attention model with no sampling temperature.
    ``timestamp_granularities[]`` is likewise accepted implicitly (unknown form
    fields are dropped by Starlette) — segment granularity is always returned.
    """
    del temperature  # accepted for wire parity; Paraformer has no sampling

    if _MODEL is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paraformer pipeline not loaded",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    tmp_path = _save_tmp(raw_bytes, suffix)

    # asyncio.to_thread cannot interrupt a running thread: on cancellation the
    # await returns while the worker still holds the file. The worker therefore
    # owns cleanup once it starts; the caller cleans up only when it never did.
    worker_started = False

    def _run() -> tuple[str, list[dict[str, Any]], float]:
        nonlocal worker_started
        worker_started = True
        try:
            return _transcribe_file(tmp_path)
        finally:
            _unlink_tmp(tmp_path)

    try:
        async with _INFERENCE_SEMAPHORE:
            text, segments, duration = await asyncio.to_thread(_run)
    except Exception as exc:
        if not worker_started:
            _unlink_tmp(tmp_path)
        logger.exception("Paraformer inference failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Paraformer inference failed: {exc}",
        ) from exc
    except BaseException:
        # Cancellation / shutdown: nothing to report, just don't leak the file.
        if not worker_started:
            _unlink_tmp(tmp_path)
        raise

    # OpenAI's plain formats are cheap to support and cost nothing to keep
    # honest; the adapter always asks for verbose_json.
    if response_format == "text":
        return PlainTextResponse(text)

    payload: dict[str, Any] = {
        "text": text,
        "language": language or _DEFAULT_LANGUAGE,
        "duration": duration,
        "model": model or _SERVED_MODEL_NAME,
    }
    if response_format != "json":
        payload["segments"] = segments
    return JSONResponse(payload)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
# funasr joins Paraformer's output tokens with spaces, so Chinese comes back as
# "今 天 天 气 不 错". The adapter passes text straight through to ASRResult, and
# from there into the transcript, embeddings and LLM prompts — all of which
# would see a spaced-out string that no Chinese tokenizer handles sensibly.
# funasr's own rich_transcription_postprocess does not fix this (verified: it
# returns the spaced string unchanged; it only strips SenseVoice tags).
#
# Only spaces *between two CJK characters* are removed, so genuine word breaks
# in mixed speech survive: "会 议 室 review 一 下" → "会议室 review 一下".
_CJK = (
    "㐀-䶿"  # CJK ext A
    "一-鿿"  # CJK unified
    "豈-﫿"  # compatibility ideographs
    "　-〿"  # CJK punctuation
    "！-｠"  # fullwidth forms
)
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def _join_cjk_spaces(text: str) -> str:
    """Drop the token-join spaces funasr inserts between CJK characters."""
    return _CJK_SPACE_RE.sub("", text)


def _audio_duration_sec(path: str) -> float:
    """Decoded duration in seconds; 0.0 when the file cannot be read."""
    try:
        import librosa  # lazy — pulls numba on first import

        return float(librosa.get_duration(path=path))
    except Exception as exc:  # pragma: no cover - depends on codec support
        logger.warning("Could not determine duration for %s: %s", path, exc)
        return 0.0


def _transcribe_file(path: str) -> tuple[str, list[dict[str, Any]], float]:
    """Run the funasr pipeline and normalize its output to the API schema."""
    res = _MODEL.generate(  # type: ignore[union-attr]
        input=path,
        cache={},
        batch_size_s=300,
        merge_vad=True,
        merge_length_s=15,
        # Without this funasr never populates ``sentence_info`` (it is gated on
        # exactly this kwarg unless a speaker model is attached), and every
        # response would collapse to a single whole-file segment. The upstream
        # funasr-server omits it and papers over the gap by interpolating fake
        # cue boundaries from character counts — we return real ones instead.
        sentence_timestamp=True,
    )
    duration = _audio_duration_sec(path)

    if not res or not isinstance(res, list) or not isinstance(res[0], dict):
        return "", [], duration

    item = res[0]
    text = _join_cjk_spaces(str(item.get("text", "")).strip())

    segments: list[dict[str, Any]] = []
    sentence_info = item.get("sentence_info")
    if isinstance(sentence_info, list):
        for idx, sent in enumerate(sentence_info):
            if not isinstance(sent, dict):
                continue
            seg_text = _join_cjk_spaces(str(sent.get("text", "")).strip())
            if not seg_text:
                continue
            start_ms = sent.get("start")
            end_ms = sent.get("end")
            try:
                start = float(start_ms) / _MS_PER_SEC
                end = float(end_ms) / _MS_PER_SEC
            except (TypeError, ValueError):
                logger.debug("Skipping sentence without usable timestamps: %s", sent)
                continue
            # No "confidence" key: Paraformer emits no per-sentence posterior.
            # The adapter averages whatever confidences it finds into
            # ASRResult.confidence, so inventing 1.0 here would hand downstream
            # code a constant dressed up as a quality signal. Omitting the key
            # lets the adapter apply its own declared fallback instead.
            segments.append({"id": idx, "start": start, "end": end, "text": seg_text})

    # No sentence-level timestamps (punc model absent, or a single short
    # utterance): still return one segment spanning the file so downstream
    # consumers get a timeline rather than nothing. The span is the real
    # decoded duration, not an invented one.
    if not segments and text:
        segments.append({"id": 0, "start": 0.0, "end": duration, "text": text})

    return text, segments, duration


if __name__ == "__main__":
    import uvicorn

    # Container entry point must listen beyond loopback.
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
