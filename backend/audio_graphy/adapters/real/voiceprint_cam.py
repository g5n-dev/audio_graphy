"""CAM++ voiceprint adapter — calls audiography-campplus-service HTTP API.

API contract (M7 architecture §4.2 / §6.2):

    POST {url}/v1/diarize             (multipart/form-data)
        - audio: WAV/MP3 file (service resamples to 16 kHz mono)
        - min_segment_sec: float (default 0.5)
        - max_speakers: int (default 10)
    Response 200:
        {
            "segments": [
                {"start_sec": float, "end_sec": float,
                 "speaker_id": "spk_0", "confidence": float | null},
                ...
            ],
            "num_speakers": int,
            "model": "cam++-zh-cn-16k",
            "duration_sec": float
        }

    POST {url}/v1/voiceprint/extract  (multipart/form-data)
        - audio: WAV/MP3 file
        - speaker_id: str (optional, propagated to the result)
        - start_sec: float (optional, server-side crop start)
        - end_sec: float (optional, server-side crop end)
    Response 200:
        {
            "voiceprint": [float, ...],  # length 192
            "dim": 192,
            "model": "cam++-zh-cn-16k",
            "duration_sec": float
        }

Behavior notes:
    - The service is responsible for resampling to 16 kHz and L2-normalizing
      the voiceprint. The adapter defensively re-checks L2 norm and dim.
    - ``diarize`` and ``extract_voiceprint`` are independent endpoints.
    - Lazy httpx.AsyncClient lifecycle (mirror vad_silero.py).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from pathlib import Path

import httpx

from audio_graphy.adapters.exceptions import (
    VoiceprintRequestError,
    VoiceprintServerError,
    VoiceprintTimeoutError,
    _redact,
)
from audio_graphy.adapters.protocols import (
    DEFAULT_MAX_SPEAKERS,
    DEFAULT_MIN_SEGMENT_SEC,
    VOICEPRINT_DIM,
    DiarizationResult,
    DiarizationSegment,
    VoiceprintAdapter,
    VoiceprintResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0  # diarize runs the whole file → heavier
_DEFAULT_MAX_CONNECT_SEC = 5.0
_DIARIZE_PATH = "/v1/diarize"
_VOICEPRINT_PATH = "/v1/voiceprint/extract"
_EXPECTED_DIM = VOICEPRINT_DIM  # L2 locked
_L2_TOLERANCE = 1e-5


class CAMPlusPlusAdapter:
    """Real voiceprint + diarization backed by campplus-service.

    Lifecycle (mirror vad_silero.py):
        - httpx.AsyncClient created lazily on first call.
        - Caller MUST invoke ``aclose()`` during application shutdown.
        - Re-entrant: after ``aclose()``, next call re-creates the client.

    Args:
        url: Base URL of campplus-service, e.g. ``http://campplus-service:8007``.
        model: Reported model identifier (default ``"cam++-zh-cn-16k"``).
        timeout: Per-request total timeout. Diarize is heavier → default 60s.
        max_connect_sec: Connect-only timeout.
    """

    def __init__(
        self,
        url: str,
        *,
        model: str = "cam++-zh-cn-16k",
        timeout: float = _DEFAULT_TIMEOUT,
        max_connect_sec: float = _DEFAULT_MAX_CONNECT_SEC,
    ) -> None:
        self._base_url = url.rstrip("/")
        self.model = model
        self.dim = _EXPECTED_DIM
        self._timeout_sec = timeout
        self._max_connect_sec = max_connect_sec
        self._client: httpx.AsyncClient | None = None

    # --------------------------------------------------------------
    # Protocol: diarize
    # --------------------------------------------------------------
    async def diarize(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = DEFAULT_MIN_SEGMENT_SEC,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
    ) -> DiarizationResult:
        """POST audio to /v1/diarize → speaker-segmented timeline.

        Raises:
            VoiceprintRequestError: HTTP 400 / 422 / file missing.
            VoiceprintTimeoutError: httpx.TimeoutException.
            VoiceprintServerError: HTTP 5xx / transport error / malformed JSON.
        """
        path = Path(audio_path)
        if not path.is_file():
            raise VoiceprintRequestError(
                f"audio file not found: {audio_path}",
                url=self._base_url,
            )

        client = self._get_client()
        full_url = f"{self._base_url}{_DIARIZE_PATH}"
        logger.debug("CAM++ diarize url=%s path=%s", _redact(full_url), audio_path)

        with path.open("rb") as fh:
            files = {"audio": (path.name, fh, "audio/wav")}
            data = {
                "min_segment_sec": str(min_segment_sec),
                "max_speakers": str(max_speakers),
            }
            try:
                resp = await client.post(full_url, files=files, data=data)
            except httpx.TimeoutException as exc:
                logger.warning("CAM++ diarize timeout url=%s err=%s", _redact(full_url), exc)
                raise VoiceprintTimeoutError(
                    f"CAM++ diarize timeout: {exc}",
                    url=self._base_url,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning(
                    "CAM++ diarize transport error url=%s err=%s",
                    _redact(full_url),
                    exc,
                )
                raise VoiceprintServerError(
                    f"CAM++ diarize transport error: {exc}",
                    url=self._base_url,
                ) from exc

        self._raise_for_status(resp, full_url, "diarize")
        return self._parse_diarize(resp, full_url)

    # --------------------------------------------------------------
    # Protocol: extract_voiceprint
    # --------------------------------------------------------------
    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> VoiceprintResult:
        """POST audio to /v1/voiceprint/extract → 192-d voiceprint.

        Raises:
            VoiceprintRequestError: HTTP 400 / 422 / file missing.
            VoiceprintTimeoutError: httpx.TimeoutException.
            VoiceprintServerError: HTTP 5xx / transport error / dim mismatch.
        """
        path = Path(audio_path)
        if not path.is_file():
            raise VoiceprintRequestError(
                f"audio file not found: {audio_path}",
                url=self._base_url,
            )

        client = self._get_client()
        full_url = f"{self._base_url}{_VOICEPRINT_PATH}"
        logger.debug(
            "CAM++ voiceprint url=%s path=%s speaker=%s",
            _redact(full_url),
            audio_path,
            speaker_id,
        )

        data: dict[str, str] = {"model": self.model}
        if speaker_id:
            data["speaker_id"] = speaker_id
        if start_sec is not None:
            data["start_sec"] = str(start_sec)
        if end_sec is not None:
            data["end_sec"] = str(end_sec)

        with path.open("rb") as fh:
            files = {"audio": (path.name, fh, "audio/wav")}
            try:
                resp = await client.post(full_url, files=files, data=data)
            except httpx.TimeoutException as exc:
                logger.warning("CAM++ voiceprint timeout url=%s err=%s", _redact(full_url), exc)
                raise VoiceprintTimeoutError(
                    f"CAM++ voiceprint timeout: {exc}",
                    url=self._base_url,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning(
                    "CAM++ voiceprint transport error url=%s err=%s",
                    _redact(full_url),
                    exc,
                )
                raise VoiceprintServerError(
                    f"CAM++ voiceprint transport error: {exc}",
                    url=self._base_url,
                ) from exc

        self._raise_for_status(resp, full_url, "voiceprint")
        return self._parse_voiceprint(resp, speaker_id, full_url)

    # --------------------------------------------------------------
    # httpx lifecycle
    # --------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
            logger.debug("CAM++ httpx client created (url=%s)", self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("CAM++ httpx client closed")

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _raise_for_status(
        self,
        resp: httpx.Response,
        full_url: str,
        op: str,
    ) -> None:
        if resp.status_code < 400:
            return
        body_preview = (resp.text or "")[:200]
        if resp.status_code in (400, 422):
            logger.warning(
                "CAM++ %s %d url=%s body=%s",
                op,
                resp.status_code,
                _redact(full_url),
                body_preview,
            )
            raise VoiceprintRequestError(
                f"CAM++ {op} {resp.status_code}: {body_preview}",
                url=self._base_url,
                status_code=resp.status_code,
            )
        logger.warning(
            "CAM++ %s %d url=%s body=%s",
            op,
            resp.status_code,
            _redact(full_url),
            body_preview,
        )
        raise VoiceprintServerError(
            f"CAM++ {op} {resp.status_code}: {body_preview}",
            url=self._base_url,
            status_code=resp.status_code,
        )

    def _parse_diarize(
        self,
        resp: httpx.Response,
        full_url: str,
    ) -> DiarizationResult:
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("CAM++ diarize non-JSON: %s", exc)
            raise VoiceprintServerError(
                f"CAM++ diarize returned non-JSON: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if not isinstance(payload, dict) or "segments" not in payload:
            raise VoiceprintServerError(
                f"CAM++ diarize JSON missing 'segments': {str(payload)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        out: list[DiarizationSegment] = []
        for seg in payload["segments"]:
            if not isinstance(seg, dict):
                continue
            try:
                out.append(
                    DiarizationSegment(
                        start_sec=float(seg["start_sec"]),
                        end_sec=float(seg["end_sec"]),
                        speaker_id=str(seg["speaker_id"]),
                        confidence=(
                            float(seg["confidence"]) if seg.get("confidence") is not None else None
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("CAM++ diarize skip malformed seg %r: %s", seg, exc)

        try:
            duration_sec = float(payload.get("duration_sec", 0.0))
        except (TypeError, ValueError):
            duration_sec = 0.0
        model = str(payload.get("model", self.model))

        # Trust server's num_speakers if present; else derive.
        try:
            num_speakers = int(payload.get("num_speakers", len({s.speaker_id for s in out})))
        except (TypeError, ValueError):
            num_speakers = len({s.speaker_id for s in out})

        logger.debug("CAM++ diarize OK segments=%d speakers=%d", len(out), num_speakers)
        return DiarizationResult(
            segments=tuple(out),
            num_speakers=num_speakers,
            model=model,
            duration_sec=duration_sec,
        )

    def _parse_voiceprint(
        self,
        resp: httpx.Response,
        speaker_id: str,
        full_url: str,
    ) -> VoiceprintResult:
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("CAM++ voiceprint non-JSON: %s", exc)
            raise VoiceprintServerError(
                f"CAM++ voiceprint returned non-JSON: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if not isinstance(payload, dict) or "voiceprint" not in payload:
            raise VoiceprintServerError(
                f"CAM++ voiceprint JSON missing 'voiceprint': {str(payload)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        raw_vec = payload["voiceprint"]
        if not isinstance(raw_vec, list) or not raw_vec:
            raise VoiceprintServerError(
                f"CAM++ voiceprint must be non-empty list, got {type(raw_vec).__name__}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        dim = int(payload.get("dim", len(raw_vec)))
        if dim != _EXPECTED_DIM:
            raise VoiceprintServerError(
                f"CAM++ voiceprint dim mismatch: expected {_EXPECTED_DIM}, got {dim}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        try:
            vector = tuple(float(x) for x in raw_vec)
        except (TypeError, ValueError) as exc:
            raise VoiceprintServerError(
                f"CAM++ voiceprint has non-float entries: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if len(vector) != _EXPECTED_DIM:
            raise VoiceprintServerError(
                f"CAM++ voiceprint length {len(vector)} != expected {_EXPECTED_DIM}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        # Cosine scoring here treats vectors as unit-norm, so an off-norm
        # vector would score against stored templates on a different scale.
        # Re-normalize rather than trust the wire.
        #
        # This does not make ``voiceprint_id`` reproducible across
        # deployments — the hash covers the exact float32 bytes, so any
        # difference in model weights, quantization or even the normalization
        # we apply here yields a different id. That id is a within-deployment
        # dedup key, not a portable identifier.
        norm = math.sqrt(sum(v * v for v in vector))
        if norm < 1e-12:
            raise VoiceprintServerError(
                "CAM++ voiceprint has zero norm; cannot normalize",
                url=self._base_url,
                status_code=resp.status_code,
            )
        if abs(norm - 1.0) > _L2_TOLERANCE:
            logger.warning(
                "CAM++ voiceprint L2 norm=%f (expected 1.0); re-normalizing",
                norm,
            )
            vector = tuple(v / norm for v in vector)

        try:
            duration_sec = float(payload.get("duration_sec", 0.0))
        except (TypeError, ValueError):
            duration_sec = 0.0

        model = str(payload.get("model", self.model))
        returned_speaker = str(payload.get("speaker_id", speaker_id))

        return VoiceprintResult(
            vector=vector,
            dim=_EXPECTED_DIM,
            model=model,
            speaker_id=returned_speaker,
            duration_sec=duration_sec,
        )


# Protocol satisfaction check (fails at import if drift).
_VOICEPRINT_PROTOCOL_CHECK: VoiceprintAdapter = CAMPlusPlusAdapter(url="http://example")

__all__: Sequence[str] = ("CAMPlusPlusAdapter",)
