"""Tests for CAMPlusPlusAdapter — diarize + extract_voiceprint + error mapping."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    VoiceprintRequestError,
    VoiceprintServerError,
    VoiceprintTimeoutError,
)
from audio_graphy.adapters.real.voiceprint_cam import CAMPlusPlusAdapter

_DIARIZE_URL = "http://cam.test/v1/diarize"
_VOICEPRINT_URL = "http://cam.test/v1/voiceprint/extract"


def _make_adapter() -> CAMPlusPlusAdapter:
    return CAMPlusPlusAdapter(url="http://cam.test")


def _fake_voiceprint(dim: int = 192) -> list[float]:
    import math

    val = 1.0 / math.sqrt(dim)
    return [val] * dim


# ============================================================
# diarize endpoint
# ============================================================


@pytest.mark.asyncio
async def test_diarize_happy(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    {"start_sec": 0.0, "end_sec": 5.0, "speaker_id": "spk_0", "confidence": 0.9},
                    {"start_sec": 5.0, "end_sec": 10.0, "speaker_id": "spk_1", "confidence": 0.85},
                ],
                "num_speakers": 2,
                "model": "cam++-zh-cn-16k",
                "duration_sec": 10.0,
            },
        )
    )
    try:
        result = await adapter.diarize(str(tmp_wav))
        assert result.num_speakers == 2
        assert len(result.segments) == 2
        assert result.segments[0].speaker_id == "spk_0"
        assert result.duration_sec == pytest.approx(10.0)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_missing_file() -> None:
    adapter = _make_adapter()
    with pytest.raises(VoiceprintRequestError):
        await adapter.diarize("/nonexistent.wav")
    await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_400(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(return_value=httpx.Response(400, text="bad"))
    try:
        with pytest.raises(VoiceprintRequestError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_500(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(return_value=httpx.Response(500, text="err"))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_timeout(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(side_effect=httpx.TimeoutException("t"))
    try:
        with pytest.raises(VoiceprintTimeoutError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_bad_json(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(return_value=httpx.Response(200, text="<html/>"))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_missing_segments_key(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(return_value=httpx.Response(200, json={"foo": "bar"}))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_skips_malformed_segments(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    {"start_sec": 0.0, "end_sec": 1.0, "speaker_id": "spk_0"},
                    {"oops": "no required fields"},
                    {"start_sec": "x", "end_sec": 2.0, "speaker_id": "spk_1"},
                ],
                "num_speakers": 1,
                "model": "m",
            },
        )
    )
    try:
        result = await adapter.diarize(str(tmp_wav))
        assert len(result.segments) == 1
    finally:
        await adapter.aclose()


# ============================================================
# extract_voiceprint endpoint
# ============================================================


@pytest.mark.asyncio
async def test_voiceprint_happy(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "voiceprint": _fake_voiceprint(),
                "dim": 192,
                "model": "cam++-zh-cn-16k",
                "duration_sec": 5.0,
                "speaker_id": "spk_0",
            },
        )
    )
    try:
        result = await adapter.extract_voiceprint(str(tmp_wav), speaker_id="spk_0")
        assert result.dim == 192
        assert len(result.vector) == 192
        assert result.speaker_id == "spk_0"
        assert result.duration_sec == pytest.approx(5.0)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_with_crop(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """start_sec / end_sec are forwarded as multipart fields."""
    adapter = _make_adapter()
    route = respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"voiceprint": _fake_voiceprint(), "dim": 192, "model": "m"},
        )
    )
    try:
        await adapter.extract_voiceprint(
            str(tmp_wav), speaker_id="spk_0", start_sec=1.5, end_sec=4.5
        )
        assert route.called
        # The request body should contain the crop parameters.
        request_body = route.calls.last.request.content
        assert b"start_sec" in request_body
        assert b"1.5" in request_body
        assert b"4.5" in request_body
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_missing_file() -> None:
    adapter = _make_adapter()
    with pytest.raises(VoiceprintRequestError):
        await adapter.extract_voiceprint("/nope.wav")
    await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_400(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(return_value=httpx.Response(400, text="bad"))
    try:
        with pytest.raises(VoiceprintRequestError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_500(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(return_value=httpx.Response(500, text="err"))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_timeout(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(side_effect=httpx.TimeoutException("t"))
    try:
        with pytest.raises(VoiceprintTimeoutError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_dim_mismatch(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"voiceprint": [0.1] * 128, "dim": 128, "model": "m"},
        )
    )
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_missing_key(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(return_value=httpx.Response(200, json={"foo": "bar"}))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_transport_error(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(side_effect=httpx.ConnectError("nope"))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    adapter = _make_adapter()
    await adapter.aclose()
    await adapter.aclose()
