"""respx tests for LLMOpenAIAdapter — 7 cases per PRD §7.2.

Cases:
- happy_strong: 200 from strong base_url → LLMResponse(model=qwen3.6-27b)
- happy_weak: 200 from weak base_url → LLMResponse(model=qwen3.6-35b-a3b)
- happy_with_cache: two calls with same cache_key → second cached=True, HTTP hit once
- err_400_bad_messages: 400 → LLMBadRequest
- err_429_rate_limit: 429 → LLMRateLimitError
- err_500_server: 500 → LLMServerError
- err_timeout: httpx.TimeoutException → LLMTimeoutError
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    LLMBadRequest,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMTruncatedResponseError,
)
from audio_graphy.adapters.real.llm_openai import LLMOpenAIAdapter

_STRONG_URL = "http://vllm-strong.test/v1/chat/completions"
_WEAK_URL = "http://vllm-weak.test/v1/chat/completions"


def _openai_response(model: str, text: str = "hello") -> dict[str, object]:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _strong_adapter() -> LLMOpenAIAdapter:
    return LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy-test-key",
        model="qwen3.6-27b",
    )


def _weak_adapter() -> LLMOpenAIAdapter:
    return LLMOpenAIAdapter(
        base_url="http://vllm-weak.test/v1",
        api_key="dummy-test-key",
        model="qwen3.6-35b-a3b",
    )


def test_transport_exposes_provider_and_model_epoch() -> None:
    adapter = LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy-test-key",
        model="served-model",
        model_epoch="weights-v2",
    )
    assert adapter.provider == "openai-compatible"
    assert adapter.model_epoch == "weights-v2"


@pytest.mark.asyncio
async def test_llm_happy_strong(respx_mock: respx.MockRouter) -> None:
    """Strong LLM 200 OK returns LLMResponse with model=qwen3.6-27b."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_response("qwen3.6-27b"),
            headers={"x-request-id": "provider-success-1"},
        )
    )
    try:
        resp = await adapter.complete([{"role": "user", "content": "hi"}])
        assert resp.model == "qwen3.6-27b"
        assert resp.cached is False
        assert resp.usage["total_tokens"] == 8
        assert len(resp.prompt_hash) == 64  # SHA-256 hex
        assert resp.text == "hello"
        assert resp.provider_request_id == "provider-success-1"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_happy_weak(respx_mock: respx.MockRouter) -> None:
    """Weak LLM 200 OK returns LLMResponse with model=qwen3.6-35b-a3b."""
    adapter = _weak_adapter()
    respx_mock.post(_WEAK_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-35b-a3b"))
    )
    try:
        resp = await adapter.complete([{"role": "user", "content": "hi"}])
        assert resp.model == "qwen3.6-35b-a3b"
        assert resp.cached is False
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_transport_does_not_own_an_unbounded_result_cache(
    respx_mock: respx.MockRouter,
) -> None:
    """Transport ignores legacy cache keys; centralized Gateway owns reuse."""
    adapter = _strong_adapter()
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    msgs = [{"role": "user", "content": "same"}]
    try:
        r1 = await adapter.complete(msgs, cache_key="k1")
        r2 = await adapter.complete(msgs, cache_key="k1")
        assert r1.cached is False
        assert r2.cached is False
        assert r1.text == r2.text
        assert r1.prompt_hash == r2.prompt_hash
        assert route.call_count == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_err_400_bad_messages(respx_mock: respx.MockRouter) -> None:
    """HTTP 400 maps to LLMBadRequest."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(400, text='{"error":"bad messages"}')
    )
    try:
        with pytest.raises(LLMBadRequest):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404, 409, 422])
async def test_llm_permanent_4xx_maps_to_bad_request(
    respx_mock: respx.MockRouter,
    status_code: int,
) -> None:
    """Permanent client errors must not enter the gateway retry loop."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(status_code, text="permanent client error")
    )
    try:
        with pytest.raises(LLMBadRequest) as exc_info:
            await adapter.complete([{"role": "user", "content": "x"}])
        assert exc_info.value.status_code == status_code
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_err_429_rate_limit(respx_mock: respx.MockRouter) -> None:
    """HTTP 429 maps to the gateway's retryable rate-limit exception."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(return_value=httpx.Response(429, text="rate limit"))
    try:
        with pytest.raises(LLMRateLimitError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 425, 500, 502, 599])
async def test_llm_transient_http_status_maps_to_retryable_error(
    respx_mock: respx.MockRouter,
    status_code: int,
) -> None:
    """Timeout/too-early/5xx responses remain retryable at the gateway."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(status_code, text="transient provider error")
    )
    try:
        with pytest.raises(LLMServerError) as exc_info:
            await adapter.complete([{"role": "user", "content": "x"}])
        assert exc_info.value.status_code == status_code
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_err_500_server(respx_mock: respx.MockRouter) -> None:
    """HTTP 500 maps to LLMServerError."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(return_value=httpx.Response(500, text="oom"))
    try:
        with pytest.raises(LLMServerError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_err_timeout(respx_mock: respx.MockRouter) -> None:
    """httpx.TimeoutException maps to LLMTimeoutError."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(side_effect=httpx.TimeoutException("read timeout"))
    try:
        with pytest.raises(LLMTimeoutError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_with_max_tokens(respx_mock: respx.MockRouter) -> None:
    """max_tokens kwarg is forwarded into the request payload (covers payload branch)."""
    adapter = _strong_adapter()
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    try:
        await adapter.complete([{"role": "user", "content": "hi"}], max_tokens=42)
        sent = route.calls.last.request.read()
        assert b'"max_tokens"' in sent
        assert b"42" in sent
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_forwards_supported_rich_generation_options(
    respx_mock: respx.MockRouter,
) -> None:
    adapter = _strong_adapter()
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    tools = [{"type": "function", "function": {"name": "extract"}}]
    response_format = {"type": "json_object"}
    try:
        await adapter.complete(
            [{"role": "user", "content": "hi"}],
            top_p=0.8,
            seed=17,
            stop=["END"],
            tools=tools,
            response_format=response_format,
        )
        sent = route.calls.last.request.read()
        assert b'"top_p":0.8' in sent
        assert b'"seed":17' in sent
        assert b'"stop":["END"]' in sent
        assert b'"tools"' in sent
        assert b'"response_format":{"type":"json_object"}' in sent
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_response_schema_is_upgraded_to_strict_provider_json_schema(
    respx_mock: respx.MockRouter,
) -> None:
    adapter = _strong_adapter()
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_response("qwen3.6-27b", '{"labels":[]}'),
        )
    )
    schema = {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
        "required": ["labels"],
        "additionalProperties": False,
    }
    try:
        await adapter.complete(
            [{"role": "user", "content": "tag this"}],
            response_format={"type": "json_object"},
            response_schema=schema,
        )
        payload = json.loads(route.calls.last.request.content)
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "audio_graphy_response",
                "strict": True,
                "schema": schema,
            },
        }
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_explicit_json_object_capability_falls_back_without_silent_drop(
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy-test-key",
        model="qwen3.6-27b",
        structured_output_capability="json_object",
    )
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_openai_response("qwen3.6-27b", '{"labels":[]}'),
        )
    )
    try:
        with caplog.at_level("WARNING"):
            await adapter.complete(
                [{"role": "user", "content": "tag this"}],
                response_schema={"type": "object"},
            )
        payload = json.loads(route.calls.last.request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "strict JSON Schema unavailable" in caplog.text
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_unsupported_structured_output_fails_before_provider_call(
    respx_mock: respx.MockRouter,
) -> None:
    adapter = LLMOpenAIAdapter(
        base_url="http://vllm-strong.test/v1",
        api_key="dummy-test-key",
        model="qwen3.6-27b",
        structured_output_capability="unsupported",
    )
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    try:
        with pytest.raises(ValueError, match="does not support structured output"):
            await adapter.complete(
                [{"role": "user", "content": "tag this"}],
                response_schema={"type": "object"},
            )
        assert route.call_count == 0
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_err_transport(respx_mock: respx.MockRouter) -> None:
    """Non-timeout httpx.HTTPError (e.g. ConnectError) maps to LLMServerError."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(side_effect=httpx.ConnectError("dns failure"))
    try:
        with pytest.raises(LLMServerError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_client_recreation_after_close(respx_mock: respx.MockRouter) -> None:
    """After aclose(), the next call lazily re-creates the httpx client (covers is_closed branch)."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    try:
        await adapter.complete([{"role": "user", "content": "first"}])
        first_client = adapter._client
        assert first_client is not None
        await adapter.aclose()
        assert adapter._client is not None and adapter._client.is_closed
        await adapter.complete([{"role": "user", "content": "second"}])
        assert adapter._client is not first_client
        assert not adapter._client.is_closed
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_close_idempotent(respx_mock: respx.MockRouter) -> None:
    """aclose() on a never-used adapter (client=None) and double-close are no-ops."""
    adapter = _strong_adapter()
    await adapter.aclose()  # client is None — should not raise
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    try:
        await adapter.complete([{"role": "user", "content": "x"}])
        await adapter.aclose()
        await adapter.aclose()  # second close on closed client — no-op
        assert adapter._client is not None and adapter._client.is_closed
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_non_json_response(respx_mock: respx.MockRouter) -> None:
    """A 200 parse failure is permanent and must not enter gateway retries."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, text="<html>gateway crashed</html>")
    )
    try:
        with pytest.raises(LLMBadRequest):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_missing_choices_in_response(respx_mock: respx.MockRouter) -> None:
    """A malformed success envelope is permanent for this logical request."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    try:
        with pytest.raises(LLMBadRequest):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_incomplete_length_response_is_not_returned_for_caching(
    respx_mock: respx.MockRouter,
) -> None:
    adapter = _strong_adapter()
    payload = _openai_response("qwen3.6-27b", text='{"partial":')
    payload["choices"][0]["finish_reason"] = "length"  # type: ignore[index]
    respx_mock.post(_STRONG_URL).mock(return_value=httpx.Response(200, json=payload))

    try:
        with pytest.raises(LLMTruncatedResponseError, match="incomplete") as exc_info:
            await adapter.complete([{"role": "user", "content": "x"}])
        assert exc_info.value.finish_reason == "length"
        assert exc_info.value.provider_request_id == "chatcmpl-x"
        assert exc_info.value.usage == {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
        }
        assert exc_info.value.billed_usage_known is True
        assert exc_info.value.unknown_billed is False
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_incomplete_length_without_usage_is_unknown_billed(
    respx_mock: respx.MockRouter,
) -> None:
    adapter = _strong_adapter()
    payload = _openai_response("qwen3.6-27b", text='{"partial":')
    payload["choices"][0]["finish_reason"] = "length"  # type: ignore[index]
    payload.pop("usage")
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(
            200,
            json=payload,
            headers={"x-request-id": "request-from-header"},
        )
    )

    try:
        with pytest.raises(LLMTruncatedResponseError) as exc_info:
            await adapter.complete([{"role": "user", "content": "x"}])
        assert exc_info.value.provider_request_id == "request-from-header"
        assert exc_info.value.usage == {}
        assert exc_info.value.billed_usage_known is False
        assert exc_info.value.unknown_billed is True
    finally:
        await adapter.aclose()
