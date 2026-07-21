"""Tests for CLAPServiceAdapter — 8 HTTP scenarios + dim validation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    CLAPRequestError,
    CLAPServerError,
    CLAPTimeoutError,
    CLAPTooLargeError,
)
from audio_graphy.adapters.real.audio_embed_clap import CLAPServiceAdapter

_CLAP_URL = "http://clap.test/v1/audio/embed"


def _make_adapter() -> CLAPServiceAdapter:
    return CLAPServiceAdapter(url="http://clap.test")


def _fake_embedding(dim: int = 512, value: float = 0.044) -> list[float]:
    """Return a fixed-length L2-normalized vector."""
    import math

    val = value if dim == 0 else value / math.sqrt(dim)
    return [val] * dim


@pytest.mark.asyncio
async def test_clap_happy_200(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "embedding": _fake_embedding(),
                "dim": 512,
                "model": "clap-htsat-base-2022",
                "duration_sec": 1.5,
            },
        )
    )
    try:
        results = await adapter.embed_audio([str(tmp_wav)])
        assert len(results) == 1
        r = results[0]
        assert r.dim == 512
        assert len(r.vector) == 512
        assert r.model == "clap-htsat-base-2022"
        assert r.duration_sec == pytest.approx(1.5)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_multiple_files(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"embedding": _fake_embedding(), "dim": 512, "model": "x"},
        )
    )
    try:
        results = await adapter.embed_audio(
            [str(tmp_wav), str(tmp_wav)], segment_ids=[3, 7]
        )
        assert len(results) == 2
        assert results[0].segment_id == 3
        assert results[1].segment_id == 7
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_empty_paths() -> None:
    adapter = _make_adapter()
    result = await adapter.embed_audio([])
    assert result == ()


@pytest.mark.asyncio
async def test_clap_missing_file() -> None:
    adapter = _make_adapter()
    with pytest.raises(CLAPRequestError):
        await adapter.embed_audio(["/nonexistent/audio.wav"])
    await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_segment_ids_mismatch(tmp_wav: Path) -> None:
    adapter = _make_adapter()
    with pytest.raises(CLAPRequestError):
        await adapter.embed_audio([str(tmp_wav)], segment_ids=[1, 2])
    await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_err_400(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(return_value=httpx.Response(400, text="bad audio"))
    try:
        with pytest.raises(CLAPRequestError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_err_413(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(return_value=httpx.Response(413, text="too big"))
    try:
        with pytest.raises(CLAPTooLargeError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_err_500(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(return_value=httpx.Response(500, text="infer fail"))
    try:
        with pytest.raises(CLAPServerError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_err_timeout(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    try:
        with pytest.raises(CLAPTimeoutError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_err_bad_json(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    try:
        with pytest.raises(CLAPServerError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_dim_mismatch(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"embedding": [0.1] * 256, "dim": 256, "model": "x"},
        )
    )
    try:
        with pytest.raises(CLAPServerError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_missing_embedding_key(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(200, json={"foo": "bar"})
    )
    try:
        with pytest.raises(CLAPServerError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_transport_error(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(side_effect=httpx.ConnectError("conn refused"))
    try:
        with pytest.raises(CLAPServerError):
            await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_clap_aclose_idempotent() -> None:
    adapter = _make_adapter()
    await adapter.aclose()
    await adapter.aclose()  # second close is no-op


@pytest.mark.asyncio
async def test_clap_recreate_after_close(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    adapter = _make_adapter()
    respx_mock.post(_CLAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"embedding": _fake_embedding(), "dim": 512, "model": "x"},
        )
    )
    try:
        await adapter.embed_audio([str(tmp_wav)])
        await adapter.aclose()
        # Re-entrant: next call recreates the client.
        await adapter.embed_audio([str(tmp_wav)])
    finally:
        await adapter.aclose()
