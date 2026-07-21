"""respx tests for FunASRAdapter — 8 cases per M5 arch §7.2.

Cases:
- happy_verbose_json: 200 with segments + duration + language
- happy_minimal_json: 200 with only text field
- err_400_bad_audio: 400 → ASRRequestError
- err_413_too_large: 413 → ASRTooLargeError
- err_422_unsupported_response_format: 422 → ASRRequestError
- err_429_rate_limit: 429 → ASRRateLimitError
- err_500_server: 500 → ASRServerError
- err_timeout: httpx.TimeoutException → ASRTimeoutError
- (extra) err_401_auth / err_non_json / err_missing_text_key / err_transport
- (extra) happy_malformed_segments / file_not_found / non_dict_payload
- (extra) reentrant_after_aclose
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    ASRAuthError,
    ASRRateLimitError,
    ASRRequestError,
    ASRServerError,
    ASRTimeoutError,
    ASRTooLargeError,
)
from audio_graphy.adapters.real.funasr import FunASRAdapter

_ASR_URL = "http://funasr.test/v1/audio/transcriptions"


def _make_adapter() -> FunASRAdapter:
    return FunASRAdapter(
        url="http://funasr.test",
        model="fun-asr-nano",
        api_key="dummy-test-key",
    )


def _verbose_json() -> dict[str, object]:
    return {
        "text": "今天我们讨论三个议题。",
        "segments": [
            {
                "id": 0,
                "start": 1.7,
                "end": 5.5,
                "text": "今天我们讨论三个议题。",
                "confidence": 0.96,
            },
            {
                "id": 1,
                "start": 6.0,
                "end": 10.1,
                "text": "首先是价格。",
                "confidence": 0.92,
            },
        ],
        "language": "zh",
        "duration": 12.1,
        "model": "fun-asr-nano",
    }


@pytest.mark.asyncio
async def test_asr_happy_verbose_json(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """200 + full verbose_json → ASRResult with 2 words, averaged confidence."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(return_value=httpx.Response(200, json=_verbose_json()))
    try:
        result = await adapter.transcribe(str(tmp_wav), language="zh")
        assert result.text == "今天我们讨论三个议题。"
        assert result.language == "zh"
        assert len(result.words) == 2
        assert result.words[0] == ("今天我们讨论三个议题。", 1.7, 5.5)
        assert result.words[1] == ("首先是价格。", 6.0, 10.1)
        # Mean of 0.96 and 0.92.
        assert result.confidence == pytest.approx(0.94)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_happy_minimal_json(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """200 + only text field → ASRResult with empty words, fallback confidence."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(200, json={"text": "仅一句文本。"})
    )
    try:
        result = await adapter.transcribe(str(tmp_wav))
        assert result.text == "仅一句文本。"
        assert result.words == ()
        assert result.confidence == pytest.approx(0.95)
        # Language falls back to the per-call default ("zh").
        assert result.language == "zh"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_400_bad_audio(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 400 → ASRRequestError with status_code=400."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(400, text="unsupported codec")
    )
    try:
        with pytest.raises(ASRRequestError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 400
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_413_too_large(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 413 → ASRTooLargeError with status_code=413."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(return_value=httpx.Response(413, text="too large"))
    try:
        with pytest.raises(ASRTooLargeError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 413
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_422_unsupported_response_format(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """HTTP 422 (unsupported response_format from server side) → ASRRequestError."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(422, text="response_format=text not supported")
    )
    try:
        with pytest.raises(ASRRequestError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 422
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_429_rate_limit(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 429 → ASRRateLimitError (M5 does not retry)."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(return_value=httpx.Response(429, text="rate limit"))
    try:
        with pytest.raises(ASRRateLimitError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 429
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_500_server(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 500 → ASRServerError with status_code=500."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(return_value=httpx.Response(500, text="infer fail"))
    try:
        with pytest.raises(ASRServerError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 500
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_timeout(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """httpx.TimeoutException → ASRTimeoutError (no status_code)."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(side_effect=httpx.TimeoutException("read timeout"))
    try:
        with pytest.raises(ASRTimeoutError):
            await adapter.transcribe(str(tmp_wav))
    finally:
        await adapter.aclose()


# ============================================================
# Extra coverage tests — push adapter above 90% line coverage.
# ============================================================


@pytest.mark.asyncio
async def test_asr_err_401_auth(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """HTTP 401 → ASRAuthError."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    try:
        with pytest.raises(ASRAuthError) as exc_info:
            await adapter.transcribe(str(tmp_wav))
        assert exc_info.value.status_code == 401
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_transport_error(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Generic httpx.HTTPError (not timeout) → ASRServerError."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        side_effect=httpx.NetworkError("connection reset")
    )
    try:
        with pytest.raises(ASRServerError):
            await adapter.transcribe(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_non_json_response(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """200 + non-JSON body → ASRServerError."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    try:
        with pytest.raises(ASRServerError):
            await adapter.transcribe(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_err_missing_text_key(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """200 + JSON without 'text' → ASRServerError."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(200, json={"segments": []})
    )
    try:
        with pytest.raises(ASRServerError):
            await adapter.transcribe(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_file_not_found() -> None:
    """Missing audio file → ASRRequestError (local, no HTTP)."""
    adapter = _make_adapter()
    try:
        with pytest.raises(ASRRequestError):
            await adapter.transcribe("/nonexistent/audio.wav")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_happy_malformed_segments_skipped(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Malformed segment entries (non-dict, missing keys, bad floats) are skipped."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "text": "ok",
                "segments": [
                    "not-a-dict",                               # skipped (non-dict)
                    {"id": 0, "start": 1.0, "end": 2.0, "text": "good"},  # ok
                    {"id": 1},                                  # skipped (missing start/end)
                    {"id": 2, "start": "bad", "end": 3.0, "text": "x"},  # skipped (bad float)
                    {"id": 3, "start": 4.0, "end": 5.0, "text": ""},     # skipped (empty text)
                ],
                "language": "zh",
            },
        )
    )
    try:
        result = await adapter.transcribe(str(tmp_wav))
        assert result.text == "ok"
        assert len(result.words) == 1
        assert result.words[0] == ("good", 1.0, 2.0)
        # No confidence collected → fallback.
        assert result.confidence == pytest.approx(0.95)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_segments_not_list_skipped(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """``segments`` field present but not a list → silently ignored."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(
            200,
            json={"text": "ok", "segments": "not-a-list"},
        )
    )
    try:
        result = await adapter.transcribe(str(tmp_wav))
        assert result.words == ()
        assert result.confidence == pytest.approx(0.95)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_asr_client_reentrant_after_aclose(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """After aclose(), the next call re-creates the httpx client (re-entrant)."""
    adapter = _make_adapter()
    respx_mock.post(_ASR_URL).mock(
        return_value=httpx.Response(200, json={"text": "first"})
    )
    try:
        result1 = await adapter.transcribe(str(tmp_wav))
        assert result1.text == "first"
        await adapter.aclose()
        # Mock needs to be re-armed since respx is per-test.
        respx_mock.post(_ASR_URL).mock(
            return_value=httpx.Response(200, json={"text": "second"})
        )
        result2 = await adapter.transcribe(str(tmp_wav))
        assert result2.text == "second"
    finally:
        await adapter.aclose()
