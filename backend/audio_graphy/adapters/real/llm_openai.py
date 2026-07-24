"""OpenAI-compatible LLM adapter — used for BOTH strong and weak vLLM instances.

API contract: docs/m4-prd.md §4.2 — POST {base_url}/chat/completions.
Same class, different ``(base_url, model)`` — see ``bundle.build_hybrid_bundle``.
``cache_key`` is caller-supplied; same key → ``LLMResponse(cached=True)`` (no HTTP).
``prompt_hash`` = MD5(model, messages), identical to ``MockLLMAdapter.compute_prompt_hash``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

import httpx

from audio_graphy.adapters.exceptions import (
    LLMBadRequest,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    _redact,
)
from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
_CHAT_COMPLETIONS_PATH = "/chat/completions"


class LLMOpenAIAdapter:
    """Real LLM backed by vLLM (OpenAI-compatible API).

    真实 LLM Adapter，对接 vLLM OpenAI-compatible 接口。同一类用于 strong / weak 两档实例。
    One instance per ``(base_url, model)``; bundle constructs strong + weak with
    independent httpx clients and caches (different hosts → different pools).
    """

    model: str

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connect_sec: float = 5.0,
    ) -> None:
        """Construct the adapter.

        Args:
            base_url: e.g. ``http://vllm-strong:8000/v1`` (with /v1).
            api_key: vLLM ignores the value but OpenAI schema requires the header.
            model: served model name, e.g. ``qwen3.6-27b``.
            timeout: total request timeout (vLLM inference can take >10s). Default 60s.
            max_connect_sec: connect-only timeout.
        """
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_sec = timeout
        self._max_connect_sec = max_connect_sec
        self._client: httpx.AsyncClient | None = None
        # In-process cache: cache_key → LLMResponse. NOT shared across instances.
        self._cache: dict[str, LLMResponse] = {}

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse:
        prompt_hash = self.compute_prompt_hash(self.model, messages)

        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.debug(
                "LLM cache HIT key=%s model=%s hash=%s",
                cache_key[:8],
                self.model,
                prompt_hash[:8],
            )
            return LLMResponse(
                text=cached.text,
                model=cached.model,
                prompt_hash=cached.prompt_hash,
                cached=True,
                usage=cached.usage,
            )

        payload: dict[str, object] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        client = self._get_client()
        full_url = f"{self._base_url}{_CHAT_COMPLETIONS_PATH}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        logger.debug(
            "LLM complete url=%s model=%s hash=%s",
            _redact(full_url),
            self.model,
            prompt_hash[:8],
        )

        try:
            resp = await client.post(full_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("LLM timeout model=%s err=%s", self.model, exc)
            raise LLMTimeoutError(
                f"LLM timeout model={self.model}: {exc}",
                url=self._base_url,
                model=self.model,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("LLM transport error model=%s err=%s", self.model, exc)
            raise LLMServerError(
                f"LLM transport error: {exc}",
                url=self._base_url,
                model=self.model,
            ) from exc

        self._raise_for_status(resp, full_url)
        text, usage = self._parse_response(resp)

        response = LLMResponse(
            text=text,
            model=self.model,
            prompt_hash=prompt_hash,
            cached=False,
            usage=usage,
        )

        if cache_key:
            self._cache[cache_key] = response
            logger.debug("LLM cached key=%s model=%s", cache_key[:8], self.model)

        logger.debug(
            "LLM OK model=%s hash=%s tokens=%s",
            self.model,
            prompt_hash[:8],
            response.usage,
        )
        return response

    @staticmethod
    def compute_prompt_hash(model: str, messages: Sequence[dict[str, str]]) -> str:
        """MD5 of (model, messages) — identical to MockLLMAdapter.compute_prompt_hash."""
        payload = json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=self._max_connect_sec),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=8),
            )
            logger.debug("LLM httpx client created (model=%s, url=%s)", self.model, self._base_url)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("LLM httpx client closed (model=%s)", self.model)

    def _raise_for_status(self, resp: httpx.Response, full_url: str) -> None:
        if resp.status_code < 400:
            return
        body_preview = (resp.text or "")[:200]
        if resp.status_code == 400:
            logger.warning("LLM 400 model=%s body=%s", self.model, body_preview)
            raise LLMBadRequest(
                f"LLM 400: {body_preview}",
                url=self._base_url,
                status_code=400,
                model=self.model,
            )
        if resp.status_code == 429:
            logger.warning("LLM 429 model=%s", self.model)
            raise LLMRateLimitError(
                "LLM 429: rate limited",
                url=self._base_url,
                status_code=429,
                model=self.model,
            )
        logger.warning("LLM %d model=%s body=%s", resp.status_code, self.model, body_preview)
        raise LLMServerError(
            f"LLM {resp.status_code}: {body_preview}",
            url=self._base_url,
            status_code=resp.status_code,
            model=self.model,
        )

    def _parse_response(self, resp: httpx.Response) -> tuple[str, dict[str, int]]:
        try:
            body = resp.json()
        except ValueError as exc:
            logger.warning("LLM non-JSON response: %s", exc)
            raise LLMServerError(
                f"LLM non-JSON response: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
            ) from exc

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMServerError(
                f"LLM JSON missing choices[0].message.content: {str(body)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
            ) from exc

        usage_raw = body.get("usage") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
            "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
            "total_tokens": int(usage_raw.get("total_tokens", 0)),
        }
        return text, usage


# Protocol satisfaction check.
_LLM_PROTOCOL_CHECK: LLMAdapter = LLMOpenAIAdapter(
    base_url="http://example/v1",
    api_key="dummy",
    model="x",
)
