"""respx tests for SileroVADAdapter — 6 cases per PRD §7.2.

Cases:
- happy_200: 200 + valid JSON → Sequence[VADSegment]
- err_400_bad_audio: 400 → VADRequestError
- err_413_too_large: 413 → VADTooLargeError
- err_500_server: 500 → VADServerError
- err_timeout: httpx.TimeoutException → VADTimeoutError
- err_bad_json: 200 + non-JSON → VADServerError
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    VADRequestError,
    VADServerError,
    VADTooLargeError,
    VADTimeoutError,
)
from audio_graphy.adapters.real.vad_silero import SileroVADAdapter

_VAD_URL = "http://silero-vad.test/v1/vad/segment"


def _make_adapter() -> SileroVADAdapter:
    return SileroVADAdapter(url="http://silero-vad.test")


@pytest.mark.asyncio
async def test_vad_happy_200(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """200 OK with two segments returns a 2-tuple of VADSegment."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    {"start_sec": 0.0, "end_sec": 5.32, "confidence": 0.95},
                    {"start_sec": 6.0, "end_sec": 10.1, "confidence": 0.88},
                ],
                "model": "silero-vad-v5",
            },
        )
    )
    try:
        segments = await adapter.segment(str(tmp_wav))
        assert len(segments) == 2
        assert segments[0].end_sec == pytest.approx(5.32)
        assert segments[1].confidence == pytest.approx(0.88)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_vad_err_400_bad_audio(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 400 maps to VADRequestError."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(return_value=httpx.Response(400, text="bad audio"))
    try:
        with pytest.raises(VADRequestError):
            await adapter.segment(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_vad_err_413_too_large(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 413 maps to VADTooLargeError."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(return_value=httpx.Response(413, text="too large"))
    try:
        with pytest.raises(VADTooLargeError):
            await adapter.segment(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_vad_err_500_server(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 500 maps to VADServerError."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(return_value=httpx.Response(500, text="infer fail"))
    try:
        with pytest.raises(VADServerError):
            await adapter.segment(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_vad_err_timeout(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """httpx.TimeoutException maps to VADTimeoutError."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(side_effect=httpx.TimeoutException("read timeout"))
    try:
        with pytest.raises(VADTimeoutError):
            await adapter.segment(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_vad_err_bad_json(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """200 + non-JSON body maps to VADServerError."""
    adapter = _make_adapter()
    respx_mock.post(_VAD_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    try:
        with pytest.raises(VADServerError):
            await adapter.segment(str(tmp_wav))
    finally:
        await adapter.aclose()
