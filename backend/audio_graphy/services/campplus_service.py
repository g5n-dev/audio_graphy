"""audiography-campplus-service — combined speech service over funasr.

One container, one model registry (funasr), three endpoints:

    POST /v1/voiceprint/extract    — audio → 192-d L2-normalized voiceprint
    POST /v1/diarize               — full audio → speaker-segmented timeline
    POST /v1/audio/transcriptions  — multipart audio → verbose_json transcript
    GET  /v1/models                — OpenAI-style model list
    GET  /health                   — liveness + per-model load state

Two independent model stacks live here, and the split is deliberate:

  * The **SV model** (``iic/speech_campplus_sv_zh-cn_16k-common``, 28 MB) backs
    ``/v1/voiceprint/extract`` and loads synchronously at startup. It is the one
    contractually verified path in this service (192-d, L2 norm 1.000000,
    server-side cropping proven to actually crop), so it must never be taken
    down by anything else in this file.
  * The **ASR pipeline** (SeACo-Paraformer + FSMN-VAD + CT-Transformer punc +
    CAM++ speaker model, ~1.2 GB, ~4 GB RSS, ~120 s warm load) backs
    ``/v1/diarize`` and ``/v1/audio/transcriptions``. It loads in the
    background so a slow or failing load degrades those two endpoints only.

Diarization used to be a lie here: funasr's registry has no standalone
diarization model, so the old code fell back to reporting the whole file as one
speaker — a response that looks successful and carries no information. That
fallback is gone. Speaker labels now come from funasr attaching ``spk_model`` to
the ASR pipeline, which emits per-sentence ``spk`` cluster indices alongside the
transcript in a single ``generate()`` pass. When the pipeline is unavailable
``/v1/diarize`` returns 503 rather than fabricating a timeline.

``spk`` is a per-file cluster index, not a stable speaker identity: ``spk_0`` in
two recordings is two different people. Mapping it to a real identity is
``/v1/voiceprint/extract``'s job, which is why both live in one container.

CPU-default, GPU-optional (L8). Set ``CAMPPLUS_DEVICE=cuda`` to enable GPU.

Standalone server — runs in its own Docker container (``campplus-service``).
Started via::

    uvicorn audio_graphy.services.campplus_service:app --host 0.0.0.0 --port 8007
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger(__name__)

_SV_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"

# The ASR pipeline, spelled out as explicit ModelScope ids rather than funasr's
# aliases so a registry change cannot silently swap a model under us. Each id
# was read back out of ``funasr.download.name_maps_from_hub.name_maps_ms`` on
# the installed funasr 1.4.0, so these are exactly what the aliases resolve to:
#     paraformer-zh -> iic/speech_seaco_paraformer_large_asr_nat-...-pytorch
#     fsmn-vad      -> iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
#     ct-punc-c     -> iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch
#     cam++         -> iic/speech_campplus_sv_zh-cn_16k-common
#
# SeACo-Paraformer specifically, not plain ``speech_paraformer-large_asr_nat-*``:
# funasr only predicts token timestamps for the SeACo and vad-punc variants, and
# speaker diarization is distributed *onto* those timestamps — without them
# funasr logs "can predict timestamp" and drops the speaker labels entirely.
_ASR_MODEL_ID = os.environ.get(
    "CAMPPLUS_ASR_MODEL",
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
)
_VAD_MODEL_ID = os.environ.get(
    "CAMPPLUS_VAD_MODEL",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
)
# The zh-only 272727 punc model (293 MB), not the cn-en 471067 one (1.1 GB):
# this is the pairing that was measured end-to-end producing correct speaker
# labels, and the smaller download matters on a disk that has already truncated
# one model mid-fetch. Punctuation is not optional here — funasr gates
# ``sentence_info`` on a punc model being present when a speaker model is
# attached ("Missing punc_model, which is required by spk_model"), so clearing
# this disables diarization, not just commas.
_PUNC_MODEL_ID = os.environ.get(
    "CAMPPLUS_PUNC_MODEL",
    "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
)
# Same weights as the SV model above. funasr loads its own copy for clustering
# (28 MB, already in the model cache) rather than sharing the SV instance,
# because AutoModel owns the batching and device placement of what it builds.
_SPK_MODEL_ID = os.environ.get("CAMPPLUS_SPK_MODEL", _SV_MODEL_ID)

# Revisions are pinned because unpinned resolution defaults to "master" in
# funasr's model builder, which has silently changed weights under deployments
# before. These four are the revisions from the upstream model cards.
# The SV model backs the one contractually verified endpoint, so it is pinned
# for the same reason the pipeline's models are — and to the same revision the
# spk branch already uses, since it is the same model id. Verified equivalent
# before pinning: master and v2.0.2 hold a byte-identical campplus_cn_common.bin
# (md5 26af1054a87994349aabcc4dffd2f0cd), so this changes no vector.
_SV_MODEL_REVISION = os.environ.get("CAMPPLUS_SV_MODEL_REVISION", "v2.0.2")
_ASR_MODEL_REVISION = os.environ.get("CAMPPLUS_ASR_MODEL_REVISION", "v2.0.4")
_VAD_MODEL_REVISION = os.environ.get("CAMPPLUS_VAD_MODEL_REVISION", "v2.0.4")
_PUNC_MODEL_REVISION = os.environ.get("CAMPPLUS_PUNC_MODEL_REVISION", "v2.0.4")
_SPK_MODEL_REVISION = os.environ.get("CAMPPLUS_SPK_MODEL_REVISION", "v2.0.2")

# The served-model name the ASR adapter sends (FUNASR_MODEL, default
# "fun-asr-nano"). It is a label, not a selector: this service hosts exactly one
# pipeline. Requests naming any other model are still served, and the response
# echoes the served name, so an existing .env keeps working.
_SERVED_MODEL_NAME = os.environ.get("FUNASR_MODEL", "fun-asr-nano")
_DEFAULT_LANGUAGE = os.environ.get("FUNASR_LANGUAGE", "zh")

_TARGET_SR = 16000
_EXPECTED_DIM = 192  # L2 locked

# funasr reports sentence start/end in integer milliseconds. Every field this
# service puts on the wire is seconds (see adapters/protocols.py), so the
# conversion is mandatory — skipping it yields boundaries 1000x too large,
# which downstream reads as a timeline longer than the recording.
_MS_PER_SEC = 1000.0

# Module-level state populated in lifespan.
_SV_MODEL: Any = None  # speaker-verification CAM++ model; loaded eagerly
_ASR_MODEL: Any = None  # ASR + VAD + punc + speaker pipeline; loaded in background
_ASR_LOAD_ERROR: str | None = None  # last ASR load failure, surfaced by /health
_DEVICE: str = "cpu"

# Inference runs off the event loop so /health and queued requests stay
# responsive, and each model stays strictly serial: concurrent forward passes
# through one model would exhaust GPU memory.
#
# One semaphore per model, not one per service. _SV_MODEL and _ASR_MODEL are
# distinct objects, so sharing a slot bought no safety — it only welded their
# latencies together. Since /v1/diarize started running the full
# Paraformer+VAD+punc+spk pass (RTF ~0.2) instead of a header read, one shared
# slot meant a 10-minute recording held it for ~2 minutes while every
# /v1/voiceprint/extract queued behind it blew CAMPlusPlusAdapter's 60 s
# timeout — and chunker.py swallows that into speaker=None, so the recording is
# silently left undiarized rather than erroring. Split, the worst case is one
# cheap SV pass running alongside one heavy ASR pass.
_ASR_SEMAPHORE = asyncio.Semaphore(1)
_SV_SEMAPHORE = asyncio.Semaphore(1)

# Serializes ASR pipeline construction. Without it the startup preload and a
# request that arrives mid-load would each build their own pipeline — ~4 GB of
# resident memory apiece on a box that has one pipeline's worth to spare.
_ASR_LOAD_LOCK = asyncio.Lock()


def _build_asr_model() -> Any:
    """Construct the funasr ASR + VAD + punc + speaker pipeline. Blocking."""
    from funasr import AutoModel

    kwargs: dict[str, Any] = {
        "model": _ASR_MODEL_ID,
        "model_revision": _ASR_MODEL_REVISION,
        "vad_model": _VAD_MODEL_ID,
        "vad_model_revision": _VAD_MODEL_REVISION,
        "device": _DEVICE,
        "disable_update": True,
    }
    if _PUNC_MODEL_ID:
        kwargs["punc_model"] = _PUNC_MODEL_ID
        kwargs["punc_model_revision"] = _PUNC_MODEL_REVISION
    if _SPK_MODEL_ID:
        kwargs["spk_model"] = _SPK_MODEL_ID
        kwargs["spk_model_revision"] = _SPK_MODEL_REVISION
    logger.info(
        "Loading ASR pipeline asr=%s vad=%s punc=%s spk=%s device=%s",
        _ASR_MODEL_ID,
        _VAD_MODEL_ID,
        _PUNC_MODEL_ID or "(disabled)",
        _SPK_MODEL_ID or "(disabled)",
        _DEVICE,
    )
    return AutoModel(**kwargs)


async def _ensure_asr_model() -> Any:
    """Return the loaded ASR pipeline, loading it if it is not up yet.

    Raises HTTPException(503) while a load is in flight or after one failed.
    Requests are not queued behind an in-flight load: a warm load takes ~120 s,
    which is past the ASR adapter's 120 s timeout and twice the voiceprint
    adapter's 60 s, so waiting would burn the caller's whole budget and still
    fail. A 503 naming the state lets it retry instead.

    A failed load is retried by the next request that gets the lock, so a
    transient download failure heals without a container restart.
    """
    global _ASR_MODEL, _ASR_LOAD_ERROR

    if _ASR_MODEL is not None:
        return _ASR_MODEL
    if _ASR_LOAD_LOCK.locked():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ASR pipeline is still loading; retry shortly",
        )

    async with _ASR_LOAD_LOCK:
        if _ASR_MODEL is not None:
            return _ASR_MODEL
        try:
            # Deliberately outside _ASR_SEMAPHORE: the load takes ~2
            # minutes warm and far longer on a cold model cache, and holding
            # the inference lock for that long would stall voiceprint
            # extraction — the one path that is verified working.
            model = await asyncio.to_thread(_build_asr_model)
        except Exception as exc:
            _ASR_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
            logger.exception("Failed to load ASR pipeline")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"ASR pipeline unavailable: {_ASR_LOAD_ERROR}",
            ) from exc
        _ASR_MODEL = model
        _ASR_LOAD_ERROR = None
        logger.info("ASR pipeline loaded (spk=%s)", _spk_attached(model))
        return model


def _spk_attached(model: Any) -> bool:
    """Whether the pipeline actually carries a speaker model.

    Read off the built pipeline rather than off ``_SPK_MODEL_ID``: a configured
    id that funasr silently declined to attach would otherwise be reported as
    working diarization, which is exactly the failure this service used to ship.
    """
    return model is not None and getattr(model, "spk_model", None) is not None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the SV model at startup; preload the ASR pipeline in background."""
    global _SV_MODEL, _ASR_MODEL, _ASR_LOAD_ERROR, _DEVICE

    _DEVICE = os.environ.get("CAMPPLUS_DEVICE", "cpu").lower()
    try:
        from funasr import AutoModel

        logger.info("Loading CAM++ SV model %s on device=%s", _SV_MODEL_ID, _DEVICE)
        _SV_MODEL = AutoModel(
            model=_SV_MODEL_ID,
            model_revision=_SV_MODEL_REVISION,
            device=_DEVICE,
        )
        logger.info("CAM++ SV model loaded")
    except Exception:
        logger.exception("Failed to load CAM++ SV model at startup; exiting.")
        sys.exit(1)

    # The ASR pipeline is ~1.2 GB of weights and ~2 minutes of warm load, so it
    # cannot block startup: /v1/voiceprint/extract has to be serving while it
    # comes up. A failure here is recorded and reported by /health, never fatal.
    preload_task: asyncio.Task[Any] | None = None
    if os.environ.get("CAMPPLUS_ASR_PRELOAD", "1").lower() not in ("0", "false", "no"):

        async def _preload() -> None:
            with contextlib.suppress(HTTPException):
                await _ensure_asr_model()

        preload_task = asyncio.create_task(_preload())

    try:
        yield
    finally:
        if preload_task is not None and not preload_task.done():
            # Cancellation does not interrupt the loading thread; it only stops
            # us waiting on it. The process is going away regardless.
            preload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preload_task

        _SV_MODEL = None
        _ASR_MODEL = None
        _ASR_LOAD_ERROR = None
        logger.info("CAM++ models released")


app = FastAPI(
    title="audiography-campplus-service",
    version="2.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + per-model load state.

    Always HTTP 200 while the process is up, and the container healthcheck is
    wired to that: the SV model is the service's floor, and an ASR pipeline that
    failed to load must not mark the container unhealthy and take voiceprint
    extraction down with it. Readiness for diarization / transcription is read
    from ``asr_loaded`` and ``spk_loaded``, not from the HTTP status.
    """
    asr_loaded = _ASR_MODEL is not None
    spk_loaded = _spk_attached(_ASR_MODEL)
    if _SV_MODEL is None:
        service_status = "error"
    elif _ASR_LOAD_ERROR is not None:
        service_status = "degraded"
    elif asr_loaded and not spk_loaded:
        # The pipeline built but carries no speaker model — funasr declined to
        # attach it, or CAMPPLUS_SPK_MODEL is empty. Nothing is in flight, so
        # this never resolves: /v1/diarize 503s for the life of the process.
        # Reporting "loading" here would hide exactly the failure spk_loaded
        # was added to expose.
        service_status = "degraded"
    elif not asr_loaded:
        service_status = "loading"
    else:
        service_status = "ok"

    return {
        "status": service_status,
        "device": _DEVICE,
        "sv_loaded": _SV_MODEL is not None,
        "asr_loaded": asr_loaded,
        "spk_loaded": spk_loaded,
        # Retained under its old name for existing probes and dashboards. It now
        # means what it always claimed to: real speaker labels are available.
        "diarize_loaded": spk_loaded,
        "asr_error": _ASR_LOAD_ERROR,
        "sv_model": _SV_MODEL_ID,
        "asr_model": _ASR_MODEL_ID,
        "vad_model": _VAD_MODEL_ID,
        "punc_model": _PUNC_MODEL_ID,
        "spk_model": _SPK_MODEL_ID,
        "served_model_name": _SERVED_MODEL_NAME,
        "dim": _EXPECTED_DIM,
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """OpenAI-style model list, for clients that probe before transcribing."""
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


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 normalize a 1-D vector; safe for zero-norm input."""
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return vec
    return vec / norm


async def _read_upload(audio: UploadFile) -> bytes:
    """Read an upload, rejecting an empty body with 400."""
    raw_bytes = await audio.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty audio upload",
        )
    return raw_bytes


@app.post("/v1/diarize")
async def diarize(
    audio: UploadFile = File(...),
    min_segment_sec: float = Form(0.5),
    max_speakers: int = Form(10),
) -> JSONResponse:
    """Speaker-segmented timeline from the funasr speaker-labelled pipeline.

    ``min_segment_sec`` drops sentences shorter than it. ``max_speakers`` bounds
    what the clusterer may return; funasr offers no exact equivalent, see
    ``_apply_max_speakers``.

    Known floor, measured in funasr's clusterer rather than inferred: fewer than
    20 speech chunks short-circuits to a single speaker regardless of content,
    so short recordings come back as one speaker even when they are not. The
    upstream model card likewise documents degradation below 30 s of audio.
    """
    raw_bytes = await _read_upload(audio)

    model = await _ensure_asr_model()
    if not _spk_attached(model):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ASR pipeline has no speaker model attached; cannot diarize",
        )

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    tmp_path = _save_tmp(raw_bytes, suffix)

    # asyncio.to_thread cannot interrupt a running thread: on cancellation the
    # await returns while the worker keeps reading the file. So the worker owns
    # cleanup once it starts, and the caller only cleans up when it never did.
    worker_started = False

    def _run() -> tuple[list[dict[str, Any]], float]:
        nonlocal worker_started
        worker_started = True
        try:
            return _diarize_with_pipeline(model, tmp_path, min_segment_sec, max_speakers)
        finally:
            _unlink_tmp(tmp_path)

    try:
        async with _ASR_SEMAPHORE:
            segments, duration = await asyncio.to_thread(_run)
    except Exception as exc:
        if not worker_started:
            _unlink_tmp(tmp_path)
        logger.exception("Diarization inference failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"diarization failed: {exc}",
        ) from exc
    except BaseException:
        if not worker_started:
            _unlink_tmp(tmp_path)
        raise

    num_speakers = len({s["speaker_id"] for s in segments})
    if num_speakers > max_speakers:
        # Reported, not clamped. Collapsing clusters here would invent speaker
        # identities that the model never asserted.
        logger.warning(
            "Diarization found %d speakers, above the requested cap of %d",
            num_speakers,
            max_speakers,
        )
    return JSONResponse(
        {
            "segments": segments,
            "num_speakers": num_speakers,
            "model": _SPK_MODEL_ID,
            "duration_sec": duration,
        }
    )


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

    raw_bytes = await _read_upload(file)
    asr_model = await _ensure_asr_model()

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    tmp_path = _save_tmp(raw_bytes, suffix)

    worker_started = False

    def _run() -> tuple[str, list[dict[str, Any]], float]:
        nonlocal worker_started
        worker_started = True
        try:
            return _transcribe_with_pipeline(asr_model, tmp_path)
        finally:
            _unlink_tmp(tmp_path)

    try:
        async with _ASR_SEMAPHORE:
            text, segments, duration = await asyncio.to_thread(_run)
    except Exception as exc:
        if not worker_started:
            _unlink_tmp(tmp_path)
        logger.exception("ASR inference failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ASR inference failed: {exc}",
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

        # Measured on the audio actually fed to the model — after any crop.
        # protocols.py documents this as a quality signal; returning a
        # hardcoded 0.0 meant every duration-based gate downstream saw "no
        # signal" rather than a duration, silently and for every extraction.
        extracted_sec = await asyncio.to_thread(_audio_duration_sec, tmp_path)

        try:
            async with _SV_SEMAPHORE:
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
            "duration_sec": extracted_sec,
        }
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
# funasr joins Paraformer's output tokens with spaces, so Chinese comes back as
# "今 天 天 气 不 错". The text flows straight into transcripts, embeddings and
# LLM prompts — all of which would see a spaced-out string that no Chinese
# tokenizer handles sensibly. funasr's own rich_transcription_postprocess does
# not fix this; it only strips SenseVoice tags.
#
# Only spaces *between two CJK characters* are removed, so genuine word breaks
# in mixed speech survive: "会 议 室 review 一 下" → "会议室 review 一下".
_CJK = (
    "㐀-䶿"  # CJK ext A
    "一-鿿"  # CJK unified
    "豈-﫿"  # compatibility ideographs
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


def _generate(model: Any, path: str) -> dict[str, Any] | None:
    """Run one funasr pass; return the single result dict, or None if empty.

    Both ``/v1/diarize`` and ``/v1/audio/transcriptions`` go through here so the
    two endpoints cannot drift onto different pipeline configurations. The kwargs
    are the combination that was measured producing correct per-sentence speaker
    labels; ``merge_vad`` is deliberately absent because it re-chunks the VAD
    output and would move the boundaries the speaker labels are aligned to.

    ``sentence_timestamp`` is inert while a speaker model is attached (funasr
    populates ``sentence_info`` on the speaker branch anyway) and load-bearing
    when one is not, which is the only reason it is passed unconditionally.

    Note what is *not* passed: ``preset_spk_num``. Despite the name it is an
    oracle — funasr forwards it as ``oracle_num`` and the clusterer then emits
    exactly that many speakers. Wiring ``max_speakers`` to it would force every
    recording to 10 speakers.
    """
    res = model.generate(
        input=path,
        cache={},
        batch_size_s=300,
        sentence_timestamp=True,
    )
    if not res or not isinstance(res, list) or not isinstance(res[0], dict):
        return None
    return res[0]


def _apply_max_speakers(model: Any, max_speakers: int) -> None:
    """Bound the speaker count the spectral clusterer may return.

    funasr exposes no per-call maximum, only the exact-count ``preset_spk_num``.
    The bound genuinely lives on the clusterer as ``max_num_spks`` (it caps the
    eigengap search window), so it is set there. Safe to mutate per request only
    because ``_ASR_SEMAPHORE`` makes pipeline inference strictly serial.

    Best-effort: a funasr version that moves this attribute must not break
    diarization, so a miss is logged and the model's own default (15) applies.
    """
    cluster = getattr(getattr(model, "cb_model", None), "spectral_cluster", None)
    if cluster is None or not hasattr(cluster, "max_num_spks"):
        logger.warning(
            "funasr clusterer exposes no max_num_spks; max_speakers=%d not enforced",
            max_speakers,
        )
        return
    cluster.max_num_spks = max(1, int(max_speakers))


def _diarize_with_pipeline(
    model: Any,
    path: str,
    min_segment_sec: float,
    max_speakers: int,
) -> tuple[list[dict[str, Any]], float]:
    """Speaker timeline from the pipeline's per-sentence ``spk`` labels.

    The model is passed in rather than read from the module global: this runs on
    a worker thread, and lifespan shutdown clears that global, so re-reading it
    here could observe ``None`` mid-inference.
    """
    _apply_max_speakers(model, max_speakers)
    item = _generate(model, path)
    duration = _audio_duration_sec(path)
    if item is None:
        return [], duration

    sentence_info = item.get("sentence_info")
    if not isinstance(sentence_info, list) or not sentence_info:
        return [], duration

    segments: list[dict[str, Any]] = []
    for sent in sentence_info:
        if not isinstance(sent, dict):
            continue
        try:
            start = float(sent["start"]) / _MS_PER_SEC
            end = float(sent["end"]) / _MS_PER_SEC
        except (KeyError, TypeError, ValueError):
            logger.debug("Skipping sentence without usable timestamps: %s", sent)
            continue
        if end - start < min_segment_sec:
            continue
        # A missing speaker label is fatal rather than defaulted to 0. Defaulting
        # would emit a well-formed single-speaker timeline carrying no speaker
        # information at all — the exact failure this service shipped for months.
        if "spk" not in sent:
            raise ValueError(
                "funasr returned sentence_info without a 'spk' label; the speaker model did not run"
            )
        segments.append(
            {
                "start_sec": start,
                "end_sec": end,
                "speaker_id": f"spk_{int(sent['spk'])}",
                # No confidence: funasr's clustering emits no per-segment
                # posterior. Defaulting to 1.0 would let callers filter on a
                # constant and believe they had filtered on a signal.
                "confidence": None,
            }
        )

    segments.sort(key=lambda s: s["start_sec"])
    return segments, duration


def _transcribe_with_pipeline(
    model: Any,
    path: str,
) -> tuple[str, list[dict[str, Any]], float]:
    """Run the funasr pipeline and normalize its output to the API schema."""
    item = _generate(model, path)
    duration = _audio_duration_sec(path)
    if item is None:
        return "", [], duration

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
            try:
                start = float(sent["start"]) / _MS_PER_SEC
                end = float(sent["end"]) / _MS_PER_SEC
            except (KeyError, TypeError, ValueError):
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
