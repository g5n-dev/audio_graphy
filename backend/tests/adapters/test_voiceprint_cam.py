"""Coverage tests for ``audio_graphy.adapters.real.voiceprint_cam``.

Uses ``respx`` to mock HTTP responses for the campplus-service so all the
error branches in ``CAMPlusPlusAdapter`` can be exercised without a live
campplus-service instance.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    VoiceprintRequestError,
    VoiceprintServerError,
    VoiceprintTimeoutError,
)
from audio_graphy.adapters.real.voiceprint_cam import CAMPlusPlusAdapter

_URL = "http://campplus-service:8007"


def _ok_diarize_payload(segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if segments is None:
        segments = [
            {"start_sec": 0.0, "end_sec": 1.5, "speaker_id": "spk_0", "confidence": 0.9}
        ]
    return {
        "segments": segments,
        "num_speakers": 1,
        "model": "cam++-zh-cn-16k",
        "duration_sec": 1.5,
    }


def _ok_voiceprint_payload(vec: list[float] | None = None) -> dict[str, Any]:
    if vec is None:
        vec = [1.0] + [0.0] * 191
    return {
        "voiceprint": vec,
        "dim": 192,
        "model": "cam++-zh-cn-16k",
        "duration_sec": 1.5,
    }


@pytest.fixture
async def adapter():
    a = CAMPlusPlusAdapter(url=_URL, timeout=10.0, max_connect_sec=2.0)
    try:
        yield a
    finally:
        await a.aclose()


# ============================================================
# diarize — happy + error paths
# ============================================================


@respx.mock
async def test_diarize_happy_path(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    respx.post(f"{_URL}/v1/diarize").respond(200, json=_ok_diarize_payload())
    result = await adapter.diarize(str(wav))
    assert result.num_speakers == 1
    assert len(result.segments) == 1
    assert result.segments[0].speaker_id == "spk_0"
    assert result.model == "cam++-zh-cn-16k"


@respx.mock
async def test_diarize_handles_malformed_segments(tmp_path, adapter):
    """Malformed segment dicts are skipped without raising."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    segments = [
        {"start_sec": 0.0, "end_sec": 1.0, "speaker_id": "spk_0"},  # OK
        "not-a-dict",  # skipped
        {"start_sec": "bad", "end_sec": 2.0},  # missing speaker + bad start
        {"start_sec": 2.0, "end_sec": 3.0, "speaker_id": "spk_1"},  # OK
    ]
    respx.post(f"{_URL}/v1/diarize").respond(
        200, json=_ok_diarize_payload(segments=segments)
    )
    result = await adapter.diarize(str(wav))
    assert len(result.segments) == 2


async def test_diarize_missing_file_raises(adapter, tmp_path):
    """Missing file raises VoiceprintRequestError."""
    missing = tmp_path / "nope.wav"
    with pytest.raises(VoiceprintRequestError, match="audio file not found"):
        await adapter.diarize(str(missing))


@respx.mock
async def test_diarize_400_raises_request_error(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").respond(400, text="bad request")
    with pytest.raises(VoiceprintRequestError, match="400"):
        await adapter.diarize(str(wav))


@respx.mock
async def test_diarize_500_raises_server_error(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").respond(500, text="oops")
    with pytest.raises(VoiceprintServerError, match="500"):
        await adapter.diarize(str(wav))


@respx.mock
async def test_diarize_timeout_raises_timeout_error(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    with pytest.raises(VoiceprintTimeoutError):
        await adapter.diarize(str(wav))


@respx.mock
async def test_diarize_http_error_raises_server_error(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(VoiceprintServerError, match="transport error"):
        await adapter.diarize(str(wav))


@respx.mock
async def test_diarize_non_json_raises_server_error(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").respond(200, text="not-json")
    with pytest.raises(VoiceprintServerError, match="non-JSON"):
        await adapter.diarize(str(wav))


@respx.mock
async def test_diarize_missing_segments_key_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/diarize").respond(200, json={"foo": "bar"})
    with pytest.raises(VoiceprintServerError, match="missing 'segments'"):
        await adapter.diarize(str(wav))


# ============================================================
# extract_voiceprint — happy + error paths
# ============================================================


@respx.mock
async def test_extract_voiceprint_happy_path(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200, json=_ok_voiceprint_payload()
    )
    result = await adapter.extract_voiceprint(str(wav), speaker_id="spk_0")
    assert result.dim == 192
    assert result.speaker_id == "spk_0"
    assert len(result.vector) == 192


async def test_extract_voiceprint_missing_file_raises(adapter, tmp_path):
    missing = tmp_path / "nope.wav"
    with pytest.raises(VoiceprintRequestError, match="audio file not found"):
        await adapter.extract_voiceprint(str(missing))


@respx.mock
async def test_extract_voiceprint_400_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(400, text="bad")
    with pytest.raises(VoiceprintRequestError, match="400"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_dim_mismatch_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200,
        json={"voiceprint": [0.1, 0.2], "dim": 2},
    )
    with pytest.raises(VoiceprintServerError, match="dim mismatch"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_missing_key_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(200, json={"foo": "bar"})
    with pytest.raises(VoiceprintServerError, match="missing 'voiceprint'"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_empty_list_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200, json={"voiceprint": [], "dim": 0}
    )
    with pytest.raises(VoiceprintServerError, match="non-empty list"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_non_float_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    bad_vec = ["bad"] + [0.0] * 191
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200, json={"voiceprint": bad_vec, "dim": 192}
    )
    with pytest.raises(VoiceprintServerError, match="non-float"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_length_mismatch_raises(tmp_path, adapter):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200, json={"voiceprint": [1.0, 0.0], "dim": 192}
    )
    with pytest.raises(VoiceprintServerError, match="length"):
        await adapter.extract_voiceprint(str(wav))


@respx.mock
async def test_extract_voiceprint_unnormalised_logs_warning(
    tmp_path, adapter, caplog
):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    bad_vec = [1.0] * 192  # norm way off
    respx.post(f"{_URL}/v1/voiceprint/extract").respond(
        200, json={"voiceprint": bad_vec, "dim": 192}
    )
    with caplog.at_level(
        "WARNING", logger="audio_graphy.adapters.real.voiceprint_cam"
    ):
        result = await adapter.extract_voiceprint(str(wav))
    assert result.dim == 192
    assert any("L2 norm" in r.message for r in caplog.records)


# ============================================================
# Lifecycle
# ============================================================


async def test_aclose_idempotent_after_no_use():
    a = CAMPlusPlusAdapter(url=_URL)
    await a.aclose()
    await a.aclose()
