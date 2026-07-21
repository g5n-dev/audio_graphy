"""Silero VAD adapter — calls jetresearch/silero-vad-server HTTP API.

API contract: docs/m4-prd.md §4.1
- POST {url}/v1/vad/segment  (multipart/form-data)
- Request fields: audio (wav file), min_segment_sec, max_segment_sec
- Response 200: {"segments": [{"start_sec": float, "end_sec": float, "confidence": float}], "model": str}
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import httpx

from audio_graphy.adapters.exceptions import (
    VADRequestError,
    VADServerError,
    VADTimeoutError,
    VADTooLargeError,
    _redact,
)
from audio_graphy.adapters.protocols import VADAdapter, VADSegment

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_VAD_PATH = "/v1/vad/segment"


class SileroVADAdapter:
    """Real VAD backed by jetresearch/silero-vad-server.

    真实 VAD Adapter，基于 jetresearch/silero-vad-server（社区维护，非 Silero 官方）。

    Lifecycle:
    - httpx.AsyncClient created lazily on first ``segment()`` call (singleton per instance).
    - Caller MUST invoke ``aclose()`` during application shutdown.
    - Re-entrant: after ``aclose()``, the next call re-creates the client.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connect_sec: float = 5.0,
    ) -> None:
        """Construct the adapter.

        Args:
            url: Base URL of Silero VAD server, e.g. ``http://silero-vad:8002``.
                 Trailing slash tolerated.
            timeout: Total request timeout (read+write). Default 30s.
            max_connect_sec: Connect-only timeout. Default 5s — fast-fail when server is down.
        """
        self._base_url = url.rstrip("/")
        self._timeout_sec = timeout
        self._max_connect_sec = max_connect_sec
        self._client: httpx.AsyncClient | None = None

    # --------------------------------------------------------------
    # Protocol method
    # --------------------------------------------------------------
    async def segment(
        self,
        audio_path: str,
        *,
        min_segment_sec: float = 0.5,
        max_segment_sec: float = 30.0,
    ) -> Sequence[VADSegment]:
        """POST audio to Silero VAD server, return voice-active segments.

        Raises:
            VADRequestError: HTTP 400 — bad audio format / file missing.
            VADTooLargeError: HTTP 413 — payload too large.
            VADServerError: HTTP 5xx / transport error / malformed JSON.
            VADTimeoutError: httpx.TimeoutException.
        """
        path = Path(audio_path)
        if not path.is_file():
            raise VADRequestError(
                f"audio file not found: {audio_path}",
                url=self._base_url,
            )

        client = self._get_client()
        full_url = f"{self._base_url}{_VAD_PATH}"
        logger.debug("VAD segment url=%s path=%s", _redact(full_url), audio_path)

        # Sync open() inside async is acceptable for typical audio sizes (<100 MB).
        with path.open("rb") as fh:
            files = {"audio": (path.name, fh, "audio/wav")}
            data = {
                "min_segment_sec": str(min_segment_sec),
                "max_segment_sec": str(max_segment_sec),
            }
            try:
                resp = await client.post(full_url, files=files, data=data)
            except httpx.TimeoutException as exc:
                logger.warning("VAD timeout url=%s err=%s", _redact(full_url), exc)
                raise VADTimeoutError(
                    f"VAD timeout: {exc}",
                    url=self._base_url,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning("VAD transport error url=%s err=%s", _redact(full_url), exc)
                raise VADServerError(
                    f"VAD transport error: {exc}",
                    url=self._base_url,
                ) from exc

        self._raise_for_status(resp, full_url)
        return self._parse_segments(resp)

    # --------------------------------------------------------------
    # httpx lifecycle
    # --------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
            logger.debug("SileroVAD httpx client created (url=%s)", self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("SileroVAD httpx client closed")

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _raise_for_status(self, resp: httpx.Response, full_url: str) -> None:
        if resp.status_code < 400:
            return
        body_preview = (resp.text or "")[:200]
        if resp.status_code == 400:
            logger.warning("VAD 400 url=%s body=%s", _redact(full_url), body_preview)
            raise VADRequestError(
                f"VAD 400: {body_preview}",
                url=self._base_url,
                status_code=400,
            )
        if resp.status_code == 413:
            logger.warning("VAD 413 url=%s", _redact(full_url))
            raise VADTooLargeError(
                "VAD 413: payload too large",
                url=self._base_url,
                status_code=413,
            )
        logger.warning("VAD %d url=%s body=%s", resp.status_code, _redact(full_url), body_preview)
        raise VADServerError(
            f"VAD {resp.status_code}: {body_preview}",
            url=self._base_url,
            status_code=resp.status_code,
        )

    def _parse_segments(self, resp: httpx.Response) -> tuple[VADSegment, ...]:
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("VAD non-JSON response: %s", exc)
            raise VADServerError(
                f"VAD returned non-JSON: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if not isinstance(payload, dict) or "segments" not in payload:
            raise VADServerError(
                f"VAD JSON missing 'segments' key: {str(payload)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        out: list[VADSegment] = []
        for seg in payload["segments"]:
            out.append(
                VADSegment(
                    start_sec=float(seg["start_sec"]),
                    end_sec=float(seg["end_sec"]),
                    confidence=float(seg.get("confidence", 1.0)),
                )
            )
        logger.debug(
            "VAD OK segments=%d model=%s",
            len(out),
            payload.get("model", "?"),
        )
        return tuple(out)


# Protocol satisfaction check (fails at import if drift).
_VAD_PROTOCOL_CHECK: VADAdapter = SileroVADAdapter(url="http://example")
