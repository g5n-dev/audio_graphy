"""Coverage tests for ``audio_graphy.adapters.real.audio_embed_clap``.

Uses ``respx`` to mock the HTTP responses for the CLAP service so all the
error branches in ``CLAPServiceAdapter`` can be exercised without a live
``clap-service`` instance.
"""

from __future__ import annotations

from typing import Any

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

_CLAP_URL = "http://clap-service:8006"


def _ok_payload(vec: list[float] | None = None, dim: int = 512) -> dict[str, Any]:
    """Build a successful CLAP response payload with a unit-norm vector."""
    if vec is None:
        # Generate a 512-d unit vector by setting one component to 1.0.
        vec = [1.0] + [0.0] * (dim - 1)
    return {
        "embedding": vec,
        "dim": dim,
        "model": "clap-htsat-base-2022",
        "duration_sec": 1.5,
    }


@pytest.fixture
async def adapter():
    """Build a CLAP adapter and ensure it closes after the test."""
    a = CLAPServiceAdapter(url=_CLAP_URL, timeout=10.0, max_connect_sec=2.0)
    try:
        yield a
    finally:
        await a.aclose()


# ============================================================
# Happy path
# ============================================================


@respx.mock
async def test_embed_audio_single_file(tmp_path, adapter):
    """Single audio file with valid response returns one AudioEmbeddingResult."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00\x00" * 100)
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200, json=_ok_payload()
    )
    results = await adapter.embed_audio([str(wav)])
    assert len(results) == 1
    r = results[0]
    assert r.dim == 512
    assert r.model == "clap-htsat-base-2022"
    assert r.duration_sec == 1.5
    assert r.segment_id is None
    assert len(r.vector) == 512


@respx.mock
async def test_embed_audio_multiple_files_parallel(tmp_path, adapter):
    """Multiple files are embedded in parallel."""
    paths = []
    for i in range(3):
        p = tmp_path / f"a{i}.wav"
        p.write_bytes(b"\x00\x00" * 100)
        paths.append(p)
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200, json=_ok_payload()
    )
    results = await adapter.embed_audio(
        [str(p) for p in paths], segment_ids=[10, 11, 12]
    )
    assert len(results) == 3
    assert {r.segment_id for r in results} == {10, 11, 12}


async def test_embed_audio_empty_paths_returns_empty(adapter):
    """Empty paths list returns empty result without HTTP call."""
    assert await adapter.embed_audio([]) == ()


async def test_embed_audio_segment_ids_length_mismatch_raises(adapter, tmp_path):
    """segment_ids length mismatch raises CLAPRequestError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    with pytest.raises(CLAPRequestError, match="segment_ids length"):
        await adapter.embed_audio([str(p)], segment_ids=[1, 2])


async def test_embed_audio_missing_file_raises(adapter, tmp_path):
    """Missing audio file raises CLAPRequestError."""
    missing = tmp_path / "nonexistent.wav"
    with pytest.raises(CLAPRequestError, match="audio file not found"):
        await adapter.embed_audio([str(missing)])


# ============================================================
# HTTP error branches
# ============================================================


@respx.mock
async def test_embed_audio_400_raises_request_error(tmp_path, adapter):
    """HTTP 400 raises CLAPRequestError with body preview."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=400, text="bad request"
    )
    with pytest.raises(CLAPRequestError, match="CLAP 400"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_413_raises_too_large(tmp_path, adapter):
    """HTTP 413 raises CLAPTooLargeError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=413, text="too large"
    )
    with pytest.raises(CLAPTooLargeError):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_500_raises_server_error(tmp_path, adapter):
    """HTTP 500 raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=500, text="internal"
    )
    with pytest.raises(CLAPServerError, match="CLAP 500"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_timeout_raises_timeout_error(tmp_path, adapter):
    """TimeoutException raises CLAPTimeoutError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    with pytest.raises(CLAPTimeoutError):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_http_error_raises_server_error(tmp_path, adapter):
    """Generic HTTPError raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").mock(
        side_effect=httpx.ConnectError("conn refused")
    )
    with pytest.raises(CLAPServerError, match="transport error"):
        await adapter.embed_audio([str(p)])


# ============================================================
# Response parsing
# ============================================================


@respx.mock
async def test_embed_audio_non_json_response_raises(tmp_path, adapter):
    """Non-JSON response raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200, text="not-json"
    )
    with pytest.raises(CLAPServerError, match="non-JSON"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_missing_embedding_key_raises(tmp_path, adapter):
    """Response without 'embedding' key raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200, json={"dim": 512}
    )
    with pytest.raises(CLAPServerError, match="missing 'embedding'"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_empty_embedding_raises(tmp_path, adapter):
    """Empty embedding list raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200, json={"embedding": [], "dim": 0}
    )
    with pytest.raises(CLAPServerError, match="non-empty list"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_dim_mismatch_raises(tmp_path, adapter):
    """Dim mismatch raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200,
        json={
            "embedding": [0.1, 0.2, 0.3],
            "dim": 3,
            "model": "wrong",
        },
    )
    with pytest.raises(CLAPServerError, match="dim mismatch"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_non_float_vector_raises(tmp_path, adapter):
    """Non-float entries in vector raise CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    bad_vec = ["not-a-number"] + [0.0] * 511
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200,
        json={"embedding": bad_vec, "dim": 512},
    )
    with pytest.raises(CLAPServerError, match="non-float"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_vector_length_mismatch_raises(tmp_path, adapter):
    """Vector shorter than dim raises CLAPServerError."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    short_vec = [1.0, 0.0]  # 2 entries but dim says 512
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200,
        json={"embedding": short_vec, "dim": 512},
    )
    with pytest.raises(CLAPServerError, match="vector length"):
        await adapter.embed_audio([str(p)])


@respx.mock
async def test_embed_audio_unnormalised_vector_logs_warning(tmp_path, adapter, caplog):
    """Vector with L2 norm != 1 logs a warning but still returns the result."""
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    # Vector of all 1.0 → norm is sqrt(512) ≈ 22.6, way off unit norm.
    bad_norm_vec = [1.0] * 512
    respx.post(f"{_CLAP_URL}/v1/audio/embed").respond(
        status_code=200,
        json={"embedding": bad_norm_vec, "dim": 512},
    )
    with caplog.at_level(
        "WARNING", logger="audio_graphy.adapters.real.audio_embed_clap"
    ):
        results = await adapter.embed_audio([str(p)])
    assert len(results) == 1
    assert any("L2 norm" in r.message for r in caplog.records)


# ============================================================
# Lifecycle
# ============================================================


async def test_aclose_idempotent_after_no_use():
    """aclose on a fresh adapter (no client) is a no-op."""
    a = CLAPServiceAdapter(url=_CLAP_URL)
    await a.aclose()  # must not raise
    await a.aclose()  # second call also safe


async def test_get_client_creates_lazily():
    """The httpx client is only created on first request."""
    a = CLAPServiceAdapter(url=_CLAP_URL)
    assert a._client is None
    client = a._get_client()
    assert client is not None
    await a.aclose()
