"""M7 QA gap-fill — voiceprint_cam.py uncovered branches.

Targets lines flagged by coverage report:
- diarize: 145-151 (transport error), 308 (non-dict segment), 323-324 (duration_sec bad),
  330-331 (num_speakers bad)
- voiceprint: 349-351 (non-JSON), 366 (empty list), 382-383 (non-float entries),
  390 (length mismatch), 399 (L2 norm warning), 406-407 (duration_sec bad)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_graphy.adapters.exceptions import (
    VoiceprintServerError,
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
# diarize branches
# ============================================================


@pytest.mark.asyncio
async def test_diarize_segment_not_dict_skipped(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """A non-dict entry in segments list is skipped silently (line 308)."""
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    "not_a_dict",  # line 308 — `if not isinstance(seg, dict): continue`
                    {"start_sec": 0.0, "end_sec": 1.0, "speaker_id": "spk_0"},
                ],
                "model": "m",
            },
        )
    )
    try:
        result = await adapter.diarize(str(tmp_wav))
        assert len(result.segments) == 1
        assert result.segments[0].speaker_id == "spk_0"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_duration_sec_bad_falls_back_to_zero(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """If duration_sec is non-numeric, fall back to 0.0 (lines 323-324)."""
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    {"start_sec": 0.0, "end_sec": 1.0, "speaker_id": "spk_0"},
                ],
                "duration_sec": "not_a_number",  # triggers TypeError → fallback
                "model": "m",
            },
        )
    )
    try:
        result = await adapter.diarize(str(tmp_wav))
        assert result.duration_sec == 0.0
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_num_speakers_bad_derives_from_segments(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """If num_speakers is non-int, derive from unique speaker_ids (lines 330-331)."""
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "segments": [
                    {"start_sec": 0.0, "end_sec": 1.0, "speaker_id": "spk_0"},
                    {"start_sec": 1.0, "end_sec": 2.0, "speaker_id": "spk_1"},
                ],
                "num_speakers": "two",  # non-int → derive
                "model": "m",
            },
        )
    )
    try:
        result = await adapter.diarize(str(tmp_wav))
        assert result.num_speakers == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diarize_transport_error_raises_server_error(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Generic httpx.HTTPError surfaces as VoiceprintServerError (lines 145-151)."""
    adapter = _make_adapter()
    respx_mock.post(_DIARIZE_URL).mock(side_effect=httpx.ConnectError("conn refused"))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.diarize(str(tmp_wav))
    finally:
        await adapter.aclose()


# ============================================================
# extract_voiceprint branches
# ============================================================


@pytest.mark.asyncio
async def test_voiceprint_non_json_response(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """Non-JSON body raises VoiceprintServerError (lines 349-351)."""
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(200, content=b"not json at all")
    )
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_empty_list_raises(respx_mock: respx.MockRouter, tmp_wav: Path) -> None:
    """Empty voiceprint list raises VoiceprintServerError (line 366)."""
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(return_value=httpx.Response(200, json={"voiceprint": []}))
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_non_float_entries_raises(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Voiceprint list with non-float entries raises (lines 382-383)."""
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"voiceprint": ["x"] * 192, "dim": 192},  # strings, not floats
        )
    )
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_length_mismatch_raises(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Voiceprint list length != dim raises (line 390)."""
    adapter = _make_adapter()
    # Server claims dim=192 but vector has only 100 entries.
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"voiceprint": _fake_voiceprint(100), "dim": 192},
        )
    )
    try:
        with pytest.raises(VoiceprintServerError):
            await adapter.extract_voiceprint(str(tmp_wav))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_l2_norm_warning_but_accepted(
    respx_mock: respx.MockRouter,
    tmp_wav: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Voiceprint with L2 norm != 1.0 logs warning but accepts (line 399-402)."""
    adapter = _make_adapter()
    # Vector with norm clearly != 1.0 (all entries = 0.5 → norm = sqrt(192)*0.5 ≈ 6.93).
    bad_vec = [0.5] * 192
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"voiceprint": bad_vec, "dim": 192, "model": "m"},
        )
    )
    try:
        with caplog.at_level("WARNING", logger="audio_graphy.adapters.real.voiceprint_cam"):
            result = await adapter.extract_voiceprint(str(tmp_wav))
        # Accepted despite bad norm.
        assert result.dim == 192
        # Warning emitted.
        assert any("L2 norm" in rec.message for rec in caplog.records)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_voiceprint_duration_sec_bad_falls_back(
    respx_mock: respx.MockRouter, tmp_wav: Path
) -> None:
    """Non-numeric duration_sec falls back to 0.0 (lines 406-407)."""
    adapter = _make_adapter()
    respx_mock.post(_VOICEPRINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "voiceprint": _fake_voiceprint(),
                "dim": 192,
                "model": "m",
                "duration_sec": "n/a",
            },
        )
    )
    try:
        result = await adapter.extract_voiceprint(str(tmp_wav))
        assert result.duration_sec == 0.0
    finally:
        await adapter.aclose()
