"""CLAP audio embedding adapter — calls audiography-clap-service HTTP API.

API contract (M7 architecture §4.1 / §6.1):

    POST {url}/v1/audio/embed    (multipart/form-data)
        - audio: WAV/MP3 file (any sample rate; service resamples to 48 kHz)
        - model: optional model id override
    Response 200:
        {
            "embedding": [float, ...],   # length 512
            "dim": 512,
            "model": "clap-htsat-base-2022",
            "duration_sec": float
        }

Behavior notes:
    - One HTTP request per audio file (no batched upload) — matches silero-vad
      and funasr patterns. ``embed_audio([p1, p2, ...])`` issues N parallel
      POSTs via httpx's async pool.
    - CLAP service is responsible for resampling to 48 kHz and L2-normalizing
      the output vector. The adapter only validates dim == 512 and that the
      returned vector is finite.
    - Lazy httpx.AsyncClient lifecycle (mirror vad_silero.py).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from pathlib import Path

import httpx

from audio_graphy.adapters.exceptions import (
    CLAPRequestError,
    CLAPServerError,
    CLAPTimeoutError,
    CLAPTooLargeError,
    _redact,
)
from audio_graphy.adapters.protocols import AudioEmbedAdapter, AudioEmbeddingResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_CONNECT_SEC = 5.0
_CLAP_PATH = "/v1/audio/embed"
_EXPECTED_DIM = 512  # L1 locked
_L2_TOLERANCE = 1e-5


class CLAPServiceAdapter:
    """Real audio embedding backed by clap-service (laion_clap HTSAT-base).

    真实 CLAP Adapter，调用独立的 ``clap-service`` FastAPI 服务。

    Lifecycle (mirror vad_silero.py):
        - httpx.AsyncClient created lazily on first ``embed_audio()`` call.
        - Caller MUST invoke ``aclose()`` during application shutdown.
        - Re-entrant: after ``aclose()``, the next call re-creates the client.

    Args:
        url: Base URL of clap-service, e.g. ``http://clap-service:8006``.
            Trailing slash tolerated.
        model: Reported model identifier (default ``"clap-htsat-base-2022"``).
        timeout: Per-request total timeout. CLAP GPU ≤ 200ms / 30s segment,
            30s ceiling is sufficient.
        max_connect_sec: Connect-only timeout for fast-fail when the service
            is down.
    """

    def __init__(
        self,
        url: str,
        *,
        model: str = "clap-htsat-base-2022",
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
    # Protocol method
    # --------------------------------------------------------------
    async def embed_audio(
        self,
        audio_paths: Sequence[str],
        *,
        segment_ids: Sequence[int | None] | None = None,
    ) -> Sequence[AudioEmbeddingResult]:
        """Embed each audio file → CLAP vector.

        Args:
            audio_paths: Sequence of audio file paths (one segment per file).
            segment_ids: Optional segment index per path. If provided, must
                be the same length as ``audio_paths``. ``None`` entries pass
                through to the result as ``segment_id=None``.

        Raises:
            CLAPRequestError: HTTP 400 / 422 / file missing.
            CLAPTooLargeError: HTTP 413.
            CLAPTimeoutError: httpx.TimeoutException.
            CLAPServerError: HTTP 5xx / transport error / malformed JSON / dim mismatch.
        """
        if segment_ids is not None and len(segment_ids) != len(audio_paths):
            raise CLAPRequestError(
                "segment_ids length must match audio_paths length "
                f"({len(segment_ids)} != {len(audio_paths)})",
                url=self._base_url,
            )

        # Pre-flight: all files must exist before issuing any HTTP call.
        for path in audio_paths:
            if not Path(path).is_file():
                raise CLAPRequestError(
                    f"audio file not found: {path}",
                    url=self._base_url,
                )

        if not audio_paths:
            return ()

        ids: list[int | None]
        if segment_ids is None:
            ids = [None] * len(audio_paths)
        else:
            ids = list(segment_ids)

        # Parallel POSTs (httpx pool limits concurrency).
        tasks = [
            self._embed_one(path, seg_id)
            for path, seg_id in zip(audio_paths, ids, strict=True)
        ]
        results = await asyncio.gather(*tasks)
        return tuple(results)

    # --------------------------------------------------------------
    # Per-file POST
    # --------------------------------------------------------------
    async def _embed_one(
        self,
        audio_path: str,
        segment_id: int | None,
    ) -> AudioEmbeddingResult:
        path = Path(audio_path)
        client = self._get_client()
        full_url = f"{self._base_url}{_CLAP_PATH}"
        logger.debug("CLAP embed url=%s path=%s", _redact(full_url), audio_path)

        with path.open("rb") as fh:
            files = {"audio": (path.name, fh, "audio/wav")}
            data = {"model": self.model}
            try:
                resp = await client.post(full_url, files=files, data=data)
            except httpx.TimeoutException as exc:
                logger.warning("CLAP timeout url=%s err=%s", _redact(full_url), exc)
                raise CLAPTimeoutError(
                    f"CLAP timeout: {exc}",
                    url=self._base_url,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning(
                    "CLAP transport error url=%s err=%s", _redact(full_url), exc
                )
                raise CLAPServerError(
                    f"CLAP transport error: {exc}",
                    url=self._base_url,
                ) from exc

        self._raise_for_status(resp, full_url)
        return self._parse(resp, segment_id, full_url)

    # --------------------------------------------------------------
    # httpx lifecycle
    # --------------------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
            logger.debug("CLAP httpx client created (url=%s)", self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("CLAP httpx client closed")

    # --------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------
    def _raise_for_status(self, resp: httpx.Response, full_url: str) -> None:
        if resp.status_code < 400:
            return
        body_preview = (resp.text or "")[:200]
        if resp.status_code in (400, 422):
            logger.warning(
                "CLAP %d url=%s body=%s",
                resp.status_code,
                _redact(full_url),
                body_preview,
            )
            raise CLAPRequestError(
                f"CLAP {resp.status_code}: {body_preview}",
                url=self._base_url,
                status_code=resp.status_code,
            )
        if resp.status_code == 413:
            logger.warning("CLAP 413 url=%s", _redact(full_url))
            raise CLAPTooLargeError(
                "CLAP 413: payload too large",
                url=self._base_url,
                status_code=413,
            )
        logger.warning(
            "CLAP %d url=%s body=%s",
            resp.status_code,
            _redact(full_url),
            body_preview,
        )
        raise CLAPServerError(
            f"CLAP {resp.status_code}: {body_preview}",
            url=self._base_url,
            status_code=resp.status_code,
        )

    def _parse(
        self,
        resp: httpx.Response,
        segment_id: int | None,
        full_url: str,
    ) -> AudioEmbeddingResult:
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("CLAP non-JSON response: %s", exc)
            raise CLAPServerError(
                f"CLAP returned non-JSON: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if not isinstance(payload, dict) or "embedding" not in payload:
            raise CLAPServerError(
                f"CLAP JSON missing 'embedding' key: {str(payload)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        raw_vec = payload["embedding"]
        if not isinstance(raw_vec, list) or not raw_vec:
            raise CLAPServerError(
                f"CLAP embedding must be non-empty list, got {type(raw_vec).__name__}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        dim = int(payload.get("dim", len(raw_vec)))
        if dim != _EXPECTED_DIM:
            raise CLAPServerError(
                f"CLAP dim mismatch: expected {_EXPECTED_DIM}, got {dim}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        try:
            vector = tuple(float(x) for x in raw_vec)
        except (TypeError, ValueError) as exc:
            raise CLAPServerError(
                f"CLAP vector has non-float entries: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
            ) from exc

        if len(vector) != _EXPECTED_DIM:
            raise CLAPServerError(
                f"CLAP vector length {len(vector)} != expected {_EXPECTED_DIM}",
                url=self._base_url,
                status_code=resp.status_code,
            )

        # Defensive L2 check — service contract says L2-normalized. We log
        # but don't fail (the spec says service normalizes; ±tolerance OK).
        norm = math.sqrt(sum(v * v for v in vector))
        if abs(norm - 1.0) > _L2_TOLERANCE:
            logger.warning(
                "CLAP vector L2 norm=%f (expected 1.0); accepting as-is",
                norm,
            )

        try:
            duration_sec = float(payload.get("duration_sec", 0.0))
        except (TypeError, ValueError):
            duration_sec = 0.0

        model = str(payload.get("model", self.model))

        return AudioEmbeddingResult(
            vector=vector,
            dim=_EXPECTED_DIM,
            model=model,
            segment_id=segment_id,
            duration_sec=duration_sec,
        )


# Protocol satisfaction check (fails at import if drift).
_CLAP_PROTOCOL_CHECK: AudioEmbedAdapter = CLAPServiceAdapter(url="http://example")

__all__: Sequence[str] = ("CLAPServiceAdapter",)
