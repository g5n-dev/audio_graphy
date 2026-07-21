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

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    LLMBadRequest,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
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


@pytest.mark.asyncio
async def test_llm_happy_strong(respx_mock: respx.MockRouter) -> None:
    """Strong LLM 200 OK returns LLMResponse with model=qwen3.6-27b."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    try:
        resp = await adapter.complete([{"role": "user", "content": "hi"}])
        assert resp.model == "qwen3.6-27b"
        assert resp.cached is False
        assert resp.usage["total_tokens"] == 8
        assert len(resp.prompt_hash) == 32  # MD5 hex
        assert resp.text == "hello"
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
async def test_llm_happy_with_cache(respx_mock: respx.MockRouter) -> None:
    """Second call with same cache_key returns cached=True; HTTP hit exactly once."""
    adapter = _strong_adapter()
    route = respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json=_openai_response("qwen3.6-27b"))
    )
    msgs = [{"role": "user", "content": "same"}]
    try:
        r1 = await adapter.complete(msgs, cache_key="k1")
        r2 = await adapter.complete(msgs, cache_key="k1")
        assert r1.cached is False
        assert r2.cached is True
        assert r1.text == r2.text
        assert r1.prompt_hash == r2.prompt_hash
        assert route.call_count == 1  # KEY assertion — HTTP hit exactly once
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
async def test_llm_err_429_rate_limit(respx_mock: respx.MockRouter) -> None:
    """HTTP 429 maps to LLMRateLimitError (no retry in M4)."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(return_value=httpx.Response(429, text="rate limit"))
    try:
        with pytest.raises(LLMRateLimitError):
            await adapter.complete([{"role": "user", "content": "x"}])
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
    """200 OK with non-JSON body maps to LLMServerError."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, text="<html>gateway crashed</html>")
    )
    try:
        with pytest.raises(LLMServerError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_llm_missing_choices_in_response(respx_mock: respx.MockRouter) -> None:
    """200 OK with valid JSON but missing choices[0].message.content → LLMServerError."""
    adapter = _strong_adapter()
    respx_mock.post(_STRONG_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    try:
        with pytest.raises(LLMServerError):
            await adapter.complete([{"role": "user", "content": "x"}])
    finally:
        await adapter.aclose()
