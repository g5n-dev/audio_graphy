"""OpenAI-compatible LLM adapter — used for BOTH strong and weak vLLM instances.

API contract: docs/m4-prd.md §4.2 — POST {base_url}/chat/completions.
Same class, different ``(base_url, model)`` — see ``bundle.build_hybrid_bundle``.
``cache_key`` remains accepted for protocol compatibility but is intentionally
ignored: centralized cache ownership belongs to ``LLMGateway``.
``prompt_hash`` = SHA-256(model, messages).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx

from audio_graphy.adapters.exceptions import (
    LLMBadRequest,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMTruncatedResponseError,
    _redact,
)
from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
_CHAT_COMPLETIONS_PATH = "/chat/completions"
StructuredOutputCapability = Literal["strict_json_schema", "json_object", "unsupported"]


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
        model_epoch: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connect_sec: float = 5.0,
        structured_output_capability: StructuredOutputCapability = "strict_json_schema",
    ) -> None:
        """Construct the adapter.

        Args:
            base_url: e.g. ``http://vllm-strong:8000/v1`` (with /v1).
            api_key: vLLM ignores the value but OpenAI schema requires the header.
            model: served model name, e.g. ``qwen3.6-27b``.
            timeout: total request timeout (vLLM inference can take >10s). Default 60s.
            max_connect_sec: connect-only timeout.
            structured_output_capability: Provider's structured-output contract.
                ``strict_json_schema`` sends the schema as an OpenAI-compatible
                strict ``json_schema`` response format. ``json_object`` is an
                explicit compatibility fallback whose output still requires
                caller-side validation. ``unsupported`` fails before I/O.
        """
        if structured_output_capability not in {
            "strict_json_schema",
            "json_object",
            "unsupported",
        }:
            raise ValueError("unsupported structured_output_capability")
        self.model = model
        self.model_epoch = model_epoch or model
        self.provider = "openai-compatible"
        self.structured_output_capability = structured_output_capability
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_sec = timeout
        self._max_connect_sec = max_connect_sec
        self._client: httpx.AsyncClient | None = None

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        top_p: float = 1.0,
        seed: int | None = None,
        stop: Sequence[str] = (),
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
        response_schema: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        del cache_key
        prompt_hash = self.compute_prompt_hash(self.model, messages)

        payload: dict[str, object] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "top_p": top_p,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if seed is not None:
            payload["seed"] = seed
        if stop:
            payload["stop"] = list(stop)
        if tools:
            payload["tools"] = list(tools)
        effective_response_format = self._response_format_payload(
            response_format=response_format,
            response_schema=response_schema,
        )
        if effective_response_format is not None:
            payload["response_format"] = effective_response_format

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
        text, usage, provider_request_id = self._parse_response(resp)

        response = LLMResponse(
            text=text,
            model=self.model,
            prompt_hash=prompt_hash,
            cached=False,
            usage=usage,
            provider_request_id=provider_request_id,
        )

        logger.debug(
            "LLM OK model=%s hash=%s tokens=%s",
            self.model,
            prompt_hash[:8],
            response.usage,
        )
        return response

    def _response_format_payload(
        self,
        *,
        response_format: Mapping[str, Any] | None,
        response_schema: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Resolve the explicit provider structured-output contract.

        A supplied schema is never silently ignored. Strict-capable providers
        receive the schema itself; explicitly configured JSON-only providers
        emit a warning and use JSON mode; unsupported providers fail before a
        network call. Existing ``response_format`` behavior is unchanged when
        no schema is supplied.
        """

        if response_schema is None:
            return dict(response_format) if response_format is not None else None
        if self.structured_output_capability == "unsupported":
            raise ValueError(f"provider model={self.model} does not support structured output")
        if self.structured_output_capability == "json_object":
            logger.warning(
                "Provider strict JSON Schema unavailable; using explicit json_object "
                "fallback model=%s",
                self.model,
            )
            return {"type": "json_object"}

        format_type = response_format.get("type") if response_format is not None else None
        if format_type not in {None, "json_object", "json_schema"}:
            raise ValueError(
                "response_schema requires response_format type json_object or json_schema"
            )
        json_schema_options = (
            response_format.get("json_schema")
            if response_format is not None and format_type == "json_schema"
            else None
        )
        if json_schema_options is not None and not isinstance(json_schema_options, Mapping):
            raise TypeError("response_format.json_schema must be a mapping")
        name = "audio_graphy_response"
        description: object | None = None
        if isinstance(json_schema_options, Mapping):
            name = str(json_schema_options.get("name") or name)
            description = json_schema_options.get("description")
        elif response_format is not None:
            # Compatibility with the earlier internal shape
            # {"type": "json_schema", "name": "..."}.
            name = str(response_format.get("name") or name)
            description = response_format.get("description")
        strict_schema: dict[str, Any] = {
            "name": name,
            "strict": True,
            "schema": dict(response_schema),
        }
        if description is not None:
            strict_schema["description"] = description
        return {"type": "json_schema", "json_schema": strict_schema}

    @staticmethod
    def compute_prompt_hash(model: str, messages: Sequence[dict[str, str]]) -> str:
        """SHA-256 of ``(model, messages)`` for transport-level correlation."""
        payload = json.dumps({"model": model, "messages": list(messages)}, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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
        status_code = resp.status_code
        body_preview = (resp.text or "")[:200]
        if status_code == 429:
            logger.warning("LLM 429 model=%s", self.model)
            raise LLMRateLimitError(
                "LLM 429: rate limited",
                url=self._base_url,
                status_code=429,
                model=self.model,
            )
        if status_code in (408, 425) or 500 <= status_code < 600:
            logger.warning("LLM %d model=%s body=%s", status_code, self.model, body_preview)
            raise LLMServerError(
                f"LLM {status_code}: {body_preview}",
                url=self._base_url,
                status_code=status_code,
                model=self.model,
            )
        if 400 <= status_code < 500:
            logger.warning("LLM %d model=%s body=%s", status_code, self.model, body_preview)
            raise LLMBadRequest(
                f"LLM {status_code}: {body_preview}",
                url=self._base_url,
                status_code=status_code,
                model=self.model,
            )
        logger.warning("LLM %d model=%s body=%s", status_code, self.model, body_preview)
        raise LLMBadRequest(
            f"LLM {status_code}: {body_preview}",
            url=self._base_url,
            status_code=status_code,
            model=self.model,
        )

    def _parse_response(
        self,
        resp: httpx.Response,
    ) -> tuple[str, dict[str, int], str | None]:
        try:
            body = resp.json()
        except ValueError as exc:
            logger.warning("LLM non-JSON response: %s", exc)
            raise LLMBadRequest(
                f"LLM non-JSON response: {exc}",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
            ) from exc

        usage = _provider_usage(body)
        provider_request_id = (
            resp.headers.get("x-request-id")
            or resp.headers.get("request-id")
            or _optional_string(body.get("id"))
        )

        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("message content must be a string")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadRequest(
                f"LLM JSON missing choices[0].message.content: {str(body)[:200]}",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
            ) from exc
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise LLMTruncatedResponseError(
                f"LLM response is incomplete (finish_reason={finish_reason!r})",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
                finish_reason=finish_reason,
                usage=usage,
                provider_request_id=provider_request_id,
            )
        if finish_reason != "stop":
            raise LLMBadRequest(
                f"LLM response is incomplete (finish_reason={finish_reason!r})",
                url=self._base_url,
                status_code=resp.status_code,
                model=self.model,
            )

        return (
            text,
            usage
            or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            provider_request_id,
        )


def _provider_usage(body: Mapping[str, Any]) -> dict[str, int] | None:
    usage_raw = body.get("usage")
    if not isinstance(usage_raw, Mapping):
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not any(key in usage_raw for key in keys):
        return None
    try:
        return {key: max(0, int(usage_raw.get(key, 0))) for key in keys}
    except (TypeError, ValueError) as exc:
        raise LLMBadRequest("LLM usage fields must be integers") from exc


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# Protocol satisfaction check.
_LLM_PROTOCOL_CHECK: LLMAdapter = LLMOpenAIAdapter(
    base_url="http://example/v1",
    api_key="dummy",
    model="x",
)
