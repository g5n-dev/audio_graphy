"""funASR adapter — calls funasr/server OpenAI-compatible HTTP API.

API contract: docs/m5-prd.md §4 — POST {url}/v1/audio/transcriptions
- Request fields (multipart/form-data): file, model, language,
  response_format=verbose_json, temperature=0.0,
  timestamp_granularities[]=segment
- Response 200 (verbose_json):
    {"text": str, "segments": [{"id": int, "start": float, "end": float,
                                "text": str, "confidence": float}, ...],
     "language": str, "duration": float, "model": str}
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from pathlib import Path

import httpx

from audio_graphy.adapters.exceptions import (
    ASRAuthError,
    ASRRateLimitError,
    ASRRequestError,
    ASRServerError,
    ASRTimeoutError,
    ASRTooLargeError,
    _redact,
)
from audio_graphy.adapters.protocols import ASRAdapter, ASRResult, VADSegment

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_CONNECT_SEC = 5.0
_ASR_PATH = "/v1/audio/transcriptions"
_FORCED_RESPONSE_FORMAT = "verbose_json"
_FALLBACK_CONFIDENCE = 0.95


class FunASRAdapter:
    """Real ASR backed by funASR-server OpenAI-compatible HTTP API.

    真实 ASR Adapter，基于 funasr/server:1.0.5（OpenAI 兼容接口）。

    Lifecycle:
    - httpx.AsyncClient created lazily on first ``transcribe()`` call.
    - Caller MUST invoke ``aclose()`` during application shutdown.
    - Re-entrant: after ``aclose()``, next call re-creates the client.
    """

    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: str = "dummy",
        timeout: float = _DEFAULT_TIMEOUT,
        max_connect_sec: float = _DEFAULT_MAX_CONNECT_SEC,
        language: str = "zh",
    ) -> None:
        """Construct the adapter.

        Args:
            url: Base URL of funASR server, e.g. ``http://funasr:8000``.
                 Trailing slash tolerated.
            model: served model name (e.g. ``fun-asr-nano`` / ``fun-asr-large``).
            api_key: Bearer token; funASR ignores the value when auth disabled.
            timeout: Total request timeout. ASR long-audio needs ≥60s. Default 120s.
            max_connect_sec: Connect-only timeout. Default 5s.
            language: BCP-47 default language. Per-call override honored.
        """
        self._base_url = url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout_sec = timeout
        self._max_connect_sec = max_connect_sec
        self._default_language = language
        self._client: httpx.AsyncClient | None = None

    # --------------------------------------------------------------
    # Protocol method
    # --------------------------------------------------------------
    async def transcribe(
        self,
        audio_path: str,
        *,
        segments: list[VADSegment] | None = None,
        language: str = "zh",
    ) -> ASRResult:
        """POST audio to funASR /v1/audio/transcriptions, return ASRResult.

        ``segments`` is ignored — funASR runs its own VAD internally.
        Protocol keeps the param for signature parity with ASRAdapter.

        Raises:
            ASRRequestError: file missing / HTTP 400 / 422.
            ASRAuthError: HTTP 401 / 403.
            ASRTooLargeError: HTTP 413.
            ASRRateLimitError: HTTP 429 (M5 does NOT retry).
            ASRServerError: HTTP 5xx / transport error / malformed JSON.
            ASRTimeoutError: httpx.TimeoutException.
        """
        del segments  # explicit ignore — funASR does its own VAD

        path = Path(audio_path)
        if not path.is_file():
            raise ASRRequestError(
                f"audio file not found: {audio_path}",
                url=self._base_url,
            )

        client = self._get_client()
        full_url = f"{self._base_url}{_ASR_PATH}"
        logger.debug(
            "ASR transcribe url=%s path=%s model=%s lang=%s",
            _redact(full_url), audio_path, self.model, language,
        )

        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "audio/wav")}
            data = {
                "model": self.model,
                "response_format": _FORCED_RESPONSE_FORMAT,
                "language": language,
                "temperature": "0.0",
                "timestamp_granularities[]": "segment",
            }
            headers = {"Authorization": f"Bearer {self._api_key}"}
            try:
                resp = await client.post(
                    full_url, files=files, data=data, headers=headers
                )
            except httpx.TimeoutException as exc:
                logger.warning("ASR timeout url=%s err=%s", _redact(full_url), exc)
                raise ASRTimeoutError(
                    f"ASR timeout: {exc}",
                    url=self._base_url,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning("ASR transport error url=%s err=%s", _redact(full_url), exc)
                raise ASRServerError(
                    f"ASR transport error: {exc}",
                    url=self._base_url,
                ) from exc

        self._raise_for_status(resp, full_url)
        return self._parse_response(resp, fallback_language=language)

    # --------------------------------------------------------------
    # httpx lifecycle
    # --------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
            logger.debug("FunASR httpx client created (url=%s)", self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent / 幂等关闭."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("FunASR httpx client closed")

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _raise_for_status(self, resp: httpx.Response, full_url: str) -> None:
        if resp.status_code < 400:
            return
        body_preview = (resp.text or "")[:200]
        if resp.status_code in (400, 422):
            logger.warning("ASR %d url=%s body=%s", resp.status_code, _redact(full_url), body_preview)
            raise ASRRequestError(
                f"ASR {resp.status_code}: {body_preview}",
                url=self._base_url,
                status_code=resp.status_code,
            )
        if resp.status_code in (401, 403):
            logger.warning("ASR %d url=%s", resp.status_code, _redact(full_url))
            raise ASRAuthError(
                f"ASR {resp.status_code}: auth rejected",
                url=self._base_url,
                status_code=resp.status_code,
            )
        if resp.status_code == 413:
            logger.warning("ASR 413 url=%s", _redact(full_url))
            raise ASRTooLargeError(
                "ASR 413: payload too large",
                url=self._base_url,
                status_code=413,
            )
        if resp.status_code == 429:
            logger.warning("ASR 429 url=%s", _redact(full_url))
            raise ASRRateLimitError(
                "ASR 429: rate limited (no retry in M5)",
                url=self._base_url,
                status_code=429,
            )
        logger.warning("ASR %d url=%s body=%s", resp.status_code, _redact(full_url), body_preview)
        raise ASRServerError(
            f"ASR {resp.status_code}: {body_preview}",
            url=self._base_url,
            status_code=resp.status_code,
        )

    def _parse_response(
        self, resp: httpx.Response, *, fallback_language: str
    ) -> ASRResult:
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("ASR non-JSON response: %s", exc)
            raise ASRServerError(
                f"ASR returned non-JSON: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if not isinstance(payload, dict) or "text" not in payload:
            raise ASRServerError(
                f"ASR JSON missing 'text' key: {str(payload)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        text = str(payload["text"])
        language = str(payload.get("language", fallback_language))

        words: list[tuple[str, float, float]] = []
        confidences: list[float] = []
        raw_segments = payload.get("segments", [])
        if isinstance(raw_segments, list):
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                seg_text = str(seg.get("text", "")).strip()
                try:
                    start = float(seg["start"])
                    end = float(seg["end"])
                except (KeyError, TypeError, ValueError):
                    logger.debug("ASR skip malformed segment: %s", seg)
                    continue
                if not seg_text:
                    continue
                words.append((seg_text, start, end))
                if "confidence" in seg:
                    with contextlib.suppress(TypeError, ValueError):
                        confidences.append(float(seg["confidence"]))

        overall = (
            sum(confidences) / len(confidences) if confidences else _FALLBACK_CONFIDENCE
        )

        logger.debug(
            "ASR OK text_len=%d segments=%d model=%s",
            len(text), len(words), payload.get("model", "?"),
        )
        return ASRResult(
            text=text,
            language=language,
            confidence=overall,
            words=tuple(words),
        )


# Protocol satisfaction check (fails at import if drift).
_ASR_PROTOCOL_CHECK: ASRAdapter = FunASRAdapter(url="http://example", model="x")

# Re-export for type-checkers consuming this module.
__all__: Sequence[str] = ("FunASRAdapter",)
