"""Tests for MockAudioEmbedAdapter — determinism + L2 norm + dim validation."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from audio_graphy.adapters.mock_audio_embed import MockAudioEmbedAdapter


@pytest.mark.asyncio
async def test_mock_embed_returns_one_result_per_path(tmp_path: Path) -> None:
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    results = await adapter.embed_audio([str(p1), str(p2)])
    assert len(results) == 2
    assert all(r.dim == 512 for r in results)


@pytest.mark.asyncio
async def test_mock_embed_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r1 = await adapter.embed_audio([str(p)])
    r2 = await adapter.embed_audio([str(p)])
    assert r1[0].vector == r2[0].vector


@pytest.mark.asyncio
async def test_mock_embed_different_paths_different_vectors(tmp_path: Path) -> None:
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.wav"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r = await adapter.embed_audio([str(p1), str(p2)])
    assert r[0].vector != r[1].vector


@pytest.mark.asyncio
async def test_mock_embed_l2_normalized(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r = await adapter.embed_audio([str(p)])
    norm = math.sqrt(sum(v * v for v in r[0].vector))
    assert norm == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_mock_embed_segment_ids(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r = await adapter.embed_audio([str(p)], segment_ids=[42])
    assert r[0].segment_id == 42


@pytest.mark.asyncio
async def test_mock_embed_segment_ids_length_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    with pytest.raises(ValueError):
        await adapter.embed_audio([str(p)], segment_ids=[1, 2])


@pytest.mark.asyncio
async def test_mock_embed_empty_input() -> None:
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r = await adapter.embed_audio([])
    assert r == []


@pytest.mark.asyncio
async def test_mock_embed_no_segment_ids_defaults_none(tmp_path: Path) -> None:
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    adapter = MockAudioEmbedAdapter(latency_ms=0)
    r = await adapter.embed_audio([str(p)])
    assert r[0].segment_id is None


def test_mock_embed_invalid_dim() -> None:
    with pytest.raises(ValueError):
        MockAudioEmbedAdapter(dim=7)  # not multiple of 8


def test_mock_embed_default_model() -> None:
    a = MockAudioEmbedAdapter()
    assert a.model == "mock-clap-htsat"
    assert a.dim == 512
