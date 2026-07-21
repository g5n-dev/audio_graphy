"""respx tests for BGEEmbedAdapter — 5 cases per PRD §7.2.

Cases:
- happy_single: 1 text → 1 × EmbeddingResult(dim=1024)
- happy_batch: 4 texts → 4 × EmbeddingResult
- err_500: 500 → EmbedServerError
- err_timeout: httpx.TimeoutException → EmbedTimeoutError
- err_dim_mismatch: server returns 512-dim → EmbedDimMismatchError (adapter expects 1024)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    EmbedDimMismatchError,
    EmbedServerError,
    EmbedTimeoutError,
)
from audio_graphy.adapters.real.embed_bge import BGEEmbedAdapter

_EMBED_URL = "http://bge-m3.test/v1/embeddings"


def _embed_response(n: int, dim: int = 1024) -> dict[str, object]:
    return {
        "model": "bge-m3",
        "data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)],
    }


def _adapter(dim: int = 1024) -> BGEEmbedAdapter:
    return BGEEmbedAdapter(url="http://bge-m3.test", dim=dim)


@pytest.mark.asyncio
async def test_embed_happy_single(respx_mock: respx.MockRouter) -> None:
    """1 text → 1 EmbeddingResult with dim=1024 and full-length vector."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embed_response(1))
    )
    try:
        results = await adapter.embed_texts(["hello"])
        assert len(results) == 1
        assert results[0].dim == 1024
        assert len(results[0].vector) == 1024
        assert results[0].model == "bge-m3"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_happy_batch(respx_mock: respx.MockRouter) -> None:
    """4 texts → 4 EmbeddingResult entries."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embed_response(4))
    )
    try:
        results = await adapter.embed_texts(["a", "b", "c", "d"])
        assert len(results) == 4
        for r in results:
            assert r.dim == 1024
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_err_500(respx_mock: respx.MockRouter) -> None:
    """HTTP 500 maps to EmbedServerError."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(return_value=httpx.Response(500, text="tei down"))
    try:
        with pytest.raises(EmbedServerError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_err_timeout(respx_mock: respx.MockRouter) -> None:
    """httpx.TimeoutException maps to EmbedTimeoutError."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    try:
        with pytest.raises(EmbedTimeoutError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_err_dim_mismatch(respx_mock: respx.MockRouter) -> None:
    """Server returns 512-dim vectors while adapter expects 1024 → EmbedDimMismatchError."""
    adapter = _adapter(dim=1024)
    respx_mock.post(_EMBED_URL).mock(
        return_value=httpx.Response(200, json=_embed_response(1, dim=512))
    )
    try:
        with pytest.raises(EmbedDimMismatchError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_empty_input_returns_empty() -> None:
    """Empty input list short-circuits to () without any HTTP call (covers early return)."""
    adapter = _adapter()
    try:
        result = await adapter.embed_texts([])
        assert result == ()
        assert adapter._client is None  # no client created
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_batch_too_large() -> None:
    """Input exceeding max_batch raises EmbedServerError (covers guard branch)."""
    adapter = BGEEmbedAdapter(url="http://bge-m3.test", dim=1024, max_batch=4)
    try:
        with pytest.raises(EmbedServerError):
            await adapter.embed_texts(["a", "b", "c", "d", "e"])  # 5 > 4
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_err_transport(respx_mock: respx.MockRouter) -> None:
    """Non-timeout httpx.HTTPError (e.g. ConnectError) maps to EmbedServerError."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(side_effect=httpx.ConnectError("dns failure"))
    try:
        with pytest.raises(EmbedServerError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_client_recreation_after_close(respx_mock: respx.MockRouter) -> None:
    """After aclose(), the next call lazily re-creates the httpx client (covers is_closed branch)."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(return_value=httpx.Response(200, json=_embed_response(1)))
    try:
        await adapter.embed_texts(["first"])
        first_client = adapter._client
        assert first_client is not None
        await adapter.aclose()
        assert adapter._client is not None and adapter._client.is_closed
        await adapter.embed_texts(["second"])
        assert adapter._client is not first_client
        assert not adapter._client.is_closed
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_close_idempotent() -> None:
    """aclose() on a never-used adapter (client=None) and double-close are no-ops."""
    adapter = _adapter()
    await adapter.aclose()  # client is None — should not raise
    assert adapter._client is None
    # Use the adapter so the client is created, then double-close
    adapter._get_client()  # type: ignore[func-returns-value]
    assert adapter._client is not None
    await adapter.aclose()
    await adapter.aclose()  # second close on closed client — no-op
    assert adapter._client is not None and adapter._client.is_closed


@pytest.mark.asyncio
async def test_embed_missing_data_key(respx_mock: respx.MockRouter) -> None:
    """200 OK with valid JSON but missing 'data' key → EmbedServerError."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    try:
        with pytest.raises(EmbedServerError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_non_json_response(respx_mock: respx.MockRouter) -> None:
    """200 OK with non-JSON body maps to EmbedServerError."""
    adapter = _adapter()
    respx_mock.post(_EMBED_URL).mock(
        return_value=httpx.Response(200, text="<html>tei crashed</html>")
    )
    try:
        with pytest.raises(EmbedServerError):
            await adapter.embed_texts(["x"])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_partial_dim_mismatch_in_batch(respx_mock: respx.MockRouter) -> None:
    """One short vector in a 2-text batch triggers EmbedDimMismatchError on iteration."""
    adapter = _adapter(dim=1024)
    payload = {
        "model": "bge-m3",
        "data": [
            {"index": 0, "embedding": [0.1] * 1024},
            {"index": 1, "embedding": [0.1] * 512},  # mismatch detected mid-batch
        ],
    }
    respx_mock.post(_EMBED_URL).mock(return_value=httpx.Response(200, json=payload))
    try:
        with pytest.raises(EmbedDimMismatchError):
            await adapter.embed_texts(["ok", "bad"])
    finally:
        await adapter.aclose()
