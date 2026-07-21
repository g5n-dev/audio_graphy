"""BGE-M3 embedding adapter — calls HuggingFace TEI (text-embeddings-inference).

API contract: docs/m4-prd.md §4.3 — POST {url}/v1/embeddings (OpenAI-compatible).
Constraints: dim fixed 1024 / single call ≤ 64 inputs (batching lands in M5).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from audio_graphy.adapters.exceptions import (
    EmbedDimMismatchError,
    EmbedServerError,
    EmbedTimeoutError,
    _redact,
)
from audio_graphy.adapters.protocols import EmbedAdapter, EmbeddingResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_EMBED_PATH = "/v1/embeddings"
_MAX_BATCH = 64


class BGEEmbedAdapter:
    """Real embedding backed by HuggingFace TEI (bge-m3).

    真实 Embedding Adapter，对接 HuggingFace TEI 服务。One instance per (url, dim).
    """

    model: str
    dim: int

    def __init__(
        self,
        url: str,
        *,
        model: str = "bge-m3",
        dim: int = 1024,
        timeout: float = _DEFAULT_TIMEOUT,
        max_batch: int = _MAX_BATCH,
        max_connect_sec: float = 5.0,
    ) -> None:
        """Construct the adapter.

        Args:
            url: TEI base URL, e.g. ``http://bge-m3:8080`` (no /v1).
            model: model name sent in request body. Default ``bge-m3``.
            dim: expected vector dim. Mismatch → EmbedDimMismatchError. Default 1024.
            timeout: total request timeout. Default 30s.
            max_batch: max texts per call; >max raises EmbedServerError (M5 adds batching).
            max_connect_sec: connect-only timeout.
        """
        self._base_url = url.rstrip("/")
        self.model = model
        self.dim = dim
        self._timeout_sec = timeout
        self._max_batch = max_batch
        self._max_connect_sec = max_connect_sec
        self._client: httpx.AsyncClient | None = None

    async def embed_texts(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        """POST texts to TEI; return per-text embedding vectors.

        Raises EmbedServerError / EmbedTimeoutError / EmbedDimMismatchError (see module doc).
        """
        if not texts:
            return ()
        if len(texts) > self._max_batch:
            raise EmbedServerError(
                f"embed batch too large: {len(texts)} > {self._max_batch} (batching lands in M5)",
                url=self._base_url,
            )

        payload = {"input": list(texts), "model": self.model}
        client = self._get_client()
        full_url = f"{self._base_url}{_EMBED_PATH}"
        logger.debug("Embed url=%s count=%d dim=%d", _redact(full_url), len(texts), self.dim)

        try:
            resp = await client.post(full_url, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("Embed timeout err=%s", exc)
            raise EmbedTimeoutError(f"embed timeout: {exc}", url=self._base_url) from exc
        except httpx.HTTPError as exc:
            logger.warning("Embed transport err=%s", exc)
            raise EmbedServerError(f"embed transport error: {exc}", url=self._base_url) from exc

        if resp.status_code >= 400:
            preview = (resp.text or "")[:200]
            logger.warning("Embed %d url=%s body=%s", resp.status_code, _redact(full_url), preview)
            raise EmbedServerError(
                f"embed {resp.status_code}: {preview}",
                url=self._base_url, status_code=resp.status_code,
            )
        return self._parse_response(resp)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
            logger.debug("Embed httpx client created (url=%s)", self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("Embed httpx client closed")

    def _parse_response(self, resp: httpx.Response) -> tuple[EmbeddingResult, ...]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise EmbedServerError(
                f"embed non-JSON: {exc}",
                url=self._base_url, status_code=resp.status_code,
            ) from exc
        try:
            data_items = body["data"]
        except (KeyError, TypeError) as exc:
            raise EmbedServerError(
                f"embed JSON missing 'data': {str(body)[:200]}",
                url=self._base_url, status_code=resp.status_code,
            ) from exc

        out: list[EmbeddingResult] = []
        for item in data_items:
            vector = item["embedding"]
            if len(vector) != self.dim:
                raise EmbedDimMismatchError(
                    f"embed dim mismatch: expected {self.dim}, got {len(vector)}",
                    url=self._base_url, status_code=resp.status_code,
                )
            out.append(EmbeddingResult(
                vector=tuple(float(x) for x in vector),
                dim=self.dim,
                model=self.model,
            ))
        logger.debug("Embed OK count=%d dim=%d", len(out), self.dim)
        return tuple(out)


_EMBED_PROTOCOL_CHECK: EmbedAdapter = BGEEmbedAdapter(url="http://example")
