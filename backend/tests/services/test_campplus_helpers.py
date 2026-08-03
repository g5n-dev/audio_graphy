"""Extra coverage tests for ``audio_graphy.services.campplus_service`` helpers.

Targets the pure-Python helper functions which don't need funasr / librosa:
- ``_save_tmp`` writes bytes to disk and returns the path.
- ``_diarize_with_pipeline`` parses funasr ``sentence_info`` into the wire
  schema: milliseconds → seconds, ``spk`` → ``spk_N``, short segments dropped.
- ``_transcribe_with_pipeline`` normalizes the same payload to the OpenAI shape.
- ``_apply_max_speakers`` bounds the clusterer and tolerates its absence.
- ``_crop_audio`` invokes librosa + soundfile to crop; stubbed via monkeypatch.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest


def _install_torch_stub() -> None:
    if "torch" in sys.modules and getattr(sys.modules["torch"], "_ag_stub", False):
        return
    torch_stub = types.ModuleType("torch")
    torch_stub._ag_stub = True  # type: ignore[attr-defined]

    class _CudaNS:
        @staticmethod
        def is_available() -> bool:
            return False

    torch_stub.cuda = _CudaNS()  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub


_install_torch_stub()

# Import once at module level — this module is stateless for our helpers.
import audio_graphy.services.campplus_service as svc  # noqa: E402


def _install_librosa_stub(load_fn: Any) -> Any:
    """Install a fake ``librosa`` module in ``sys.modules``; return cleanup fn."""
    librosa_stub = types.ModuleType("librosa")
    librosa_stub.load = load_fn  # type: ignore[attr-defined]
    sys.modules["librosa"] = librosa_stub

    def _cleanup() -> None:
        sys.modules.pop("librosa", None)

    return _cleanup


def _install_soundfile_stub(write_fn: Any) -> Any:
    sf_stub = types.ModuleType("soundfile")
    sf_stub.write = write_fn  # type: ignore[attr-defined]
    sys.modules["soundfile"] = sf_stub

    def _cleanup() -> None:
        sys.modules.pop("soundfile", None)

    return _cleanup


@pytest.fixture(autouse=True)
def _cleanup_stubs() -> Any:
    """Remove librosa / soundfile stubs after each test so other tests see real state."""
    saved_librosa = sys.modules.get("librosa")
    saved_sf = sys.modules.get("soundfile")
    yield
    # Restore prior state — if no real librosa was installed, pop our stub.
    if saved_librosa is None:
        sys.modules.pop("librosa", None)
    else:
        sys.modules["librosa"] = saved_librosa
    if saved_sf is None:
        sys.modules.pop("soundfile", None)
    else:
        sys.modules["soundfile"] = saved_sf


# ============================================================
# _save_tmp
# ============================================================


def test_save_tmp_writes_bytes_and_returns_path() -> None:
    path = svc._save_tmp(b"hello-bytes", ".wav")
    try:
        assert path.endswith(".wav")
        with open(path, "rb") as f:
            assert f.read() == b"hello-bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_save_tmp_honors_suffix() -> None:
    path = svc._save_tmp(b"x", ".mp3")
    try:
        assert path.endswith(".mp3")
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ============================================================
# _diarize_with_pipeline
# ============================================================


def _install_duration_stub(seconds: float) -> None:
    """Stub librosa.get_duration, which is how the service reads duration."""
    _install_librosa_stub(lambda *a, **kw: None)
    sys.modules["librosa"].get_duration = lambda **kw: seconds  # type: ignore[attr-defined]


def _pipeline(payload: Any) -> Any:
    """A fake funasr AutoModel whose generate() returns ``payload``."""
    return types.SimpleNamespace(generate=lambda **kw: payload)


def test_diarize_pipeline_empty_response_returns_empty() -> None:
    _install_duration_stub(1.0)
    segs, dur = svc._diarize_with_pipeline(_pipeline([]), "/fake", 0.5, 5)
    assert segs == []
    assert dur == pytest.approx(1.0)


def test_diarize_pipeline_non_list_response_returns_empty() -> None:
    _install_duration_stub(1.0)
    segs, _ = svc._diarize_with_pipeline(_pipeline("not-a-list"), "/fake", 0.5, 5)
    assert segs == []


def test_diarize_pipeline_missing_sentence_info_returns_empty() -> None:
    _install_duration_stub(1.0)
    segs, _ = svc._diarize_with_pipeline(_pipeline([{"text": "hi"}]), "/fake", 0.5, 5)
    assert segs == []


def test_diarize_pipeline_converts_milliseconds_to_seconds() -> None:
    """funasr reports ms; the wire schema is seconds. 1000x errors live here."""
    _install_duration_stub(51.663)

    # Verbatim shape from the real pipeline: integer ms, 0-based int spk.
    sentence_info = [
        {"text": "嗯，", "start": 410, "end": 23430, "spk": 0},
        {"text": "对。", "start": 24270, "end": 34690, "spk": 1},
    ]
    segs, dur = svc._diarize_with_pipeline(
        _pipeline([{"sentence_info": sentence_info}]), "/f", 0.5, 5
    )

    assert dur == pytest.approx(51.663)
    assert [s["start_sec"] for s in segs] == [pytest.approx(0.410), pytest.approx(24.270)]
    assert [s["end_sec"] for s in segs] == [pytest.approx(23.430), pytest.approx(34.690)]
    assert [s["speaker_id"] for s in segs] == ["spk_0", "spk_1"]
    # Never fabricated: funasr's clustering emits no per-segment posterior.
    assert all(s["confidence"] is None for s in segs)
    # Every boundary must be inside the recording — the tripwire for ms/sec drift.
    assert all(s["end_sec"] <= dur for s in segs)


def test_diarize_pipeline_drops_short_segments_and_sorts() -> None:
    _install_duration_stub(1.0)
    sentence_info = [
        {"start": 900, "end": 1000, "spk": 0},  # 0.1s < 0.5 → dropped
        {"start": 0, "end": 800, "spk": 1},  # 0.8s ≥ 0.5 → kept
    ]
    segs, _ = svc._diarize_with_pipeline(
        _pipeline([{"sentence_info": sentence_info}]), "/f", 0.5, 5
    )
    assert len(segs) == 1
    assert segs[0]["speaker_id"] == "spk_1"
    assert segs[0]["start_sec"] == 0.0


def test_diarize_pipeline_raises_when_speaker_label_missing() -> None:
    """A missing spk must fail loudly, not collapse to a fake single speaker."""
    _install_duration_stub(2.0)
    sentence_info = [{"start": 0, "end": 2000, "text": "hi"}]  # no "spk"
    with pytest.raises(ValueError, match="spk"):
        svc._diarize_with_pipeline(_pipeline([{"sentence_info": sentence_info}]), "/f", 0.5, 5)


def test_diarize_pipeline_skips_unparsable_timestamps() -> None:
    _install_duration_stub(2.0)
    sentence_info = [
        {"start": "x", "end": 2000, "spk": 0},
        {"start": 0, "end": 2000, "spk": 1},
    ]
    segs, _ = svc._diarize_with_pipeline(
        _pipeline([{"sentence_info": sentence_info}]), "/f", 0.5, 5
    )
    assert [s["speaker_id"] for s in segs] == ["spk_1"]


# ============================================================
# _apply_max_speakers
# ============================================================


def test_apply_max_speakers_bounds_the_clusterer() -> None:
    """The cap lands on the attribute that actually bounds funasr's search."""
    cluster = types.SimpleNamespace(max_num_spks=15)
    model = types.SimpleNamespace(cb_model=types.SimpleNamespace(spectral_cluster=cluster))
    svc._apply_max_speakers(model, 3)
    assert cluster.max_num_spks == 3


def test_apply_max_speakers_floors_at_one() -> None:
    cluster = types.SimpleNamespace(max_num_spks=15)
    model = types.SimpleNamespace(cb_model=types.SimpleNamespace(spectral_cluster=cluster))
    svc._apply_max_speakers(model, 0)
    assert cluster.max_num_spks == 1


def test_apply_max_speakers_tolerates_missing_clusterer(caplog) -> None:
    """A funasr version that moves the attribute must not break diarization."""
    with caplog.at_level("WARNING", logger="audio_graphy.services.campplus_service"):
        svc._apply_max_speakers(types.SimpleNamespace(), 4)
    assert any("max_num_spks" in r.message for r in caplog.records)


# ============================================================
# _transcribe_with_pipeline / _join_cjk_spaces
# ============================================================


def test_join_cjk_spaces_removes_only_cjk_token_joins() -> None:
    assert svc._join_cjk_spaces("今 天 天 气 不 错") == "今天天气不错"
    assert svc._join_cjk_spaces("会 议 室 review 一 下") == "会议室 review 一下"


def test_transcribe_pipeline_emits_openai_segments_in_seconds() -> None:
    _install_duration_stub(10.0)
    payload = [
        {
            "text": "今 天 好。 明 天 也 好。",
            "sentence_info": [
                {"text": "今 天 好。", "start": 410, "end": 5500, "spk": 0},
                {"text": "  ", "start": 5500, "end": 5600, "spk": 0},  # blank → dropped
                {"text": "明 天 也 好。", "start": 6000, "end": 9900, "spk": 1},
            ],
        }
    ]
    text, segments, duration = svc._transcribe_with_pipeline(_pipeline(payload), "/f")

    assert text == "今天好。明天也好。"
    assert duration == pytest.approx(10.0)
    assert [s["id"] for s in segments] == [0, 2]  # index is the source position
    assert segments[0]["start"] == pytest.approx(0.410)
    assert segments[1]["end"] == pytest.approx(9.900)
    # No fabricated confidence — the adapter applies its own declared fallback.
    assert all("confidence" not in s for s in segments)


def test_transcribe_pipeline_falls_back_to_whole_file_segment() -> None:
    """Text without usable sentence timestamps still yields a real timeline."""
    _install_duration_stub(4.0)
    text, segments, duration = svc._transcribe_with_pipeline(_pipeline([{"text": "你 好"}]), "/f")
    assert text == "你好"
    assert segments == [{"id": 0, "start": 0.0, "end": 4.0, "text": "你好"}]
    assert duration == pytest.approx(4.0)


def test_transcribe_pipeline_empty_result_returns_blank() -> None:
    _install_duration_stub(3.0)
    text, segments, duration = svc._transcribe_with_pipeline(_pipeline([]), "/f")
    assert text == ""
    assert segments == []
    assert duration == pytest.approx(3.0)


# ============================================================
# _crop_audio
# ============================================================


def test_crop_audio_invokes_librosa_and_soundfile(tmp_path) -> None:
    """Crop writes a new tmp file via librosa + soundfile and unlinks source."""

    def _fake_load(path: str, sr: int, mono: bool, offset: float, duration):
        return [0.0] * 100, 48_000

    _install_librosa_stub(_fake_load)

    sf_calls: dict[str, Any] = {}

    def _fake_sf_write(name: str, y: Any, sr: int) -> None:
        sf_calls["name"] = name
        sf_calls["sr"] = sr
        with open(name, "wb") as f:
            f.write(b"fake-wav-bytes")

    _install_soundfile_stub(_fake_sf_write)

    src = tmp_path / "src.wav"
    src.write_bytes(b"source")

    out_path = svc._crop_audio(str(src), 0.0, 1.0)
    try:
        assert os.path.exists(out_path)
        assert not src.exists()
        assert sf_calls["sr"] == 48_000
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_crop_audio_handles_unlink_failure(tmp_path, caplog) -> None:
    """If source unlink fails, crop returns the new path and logs the residue."""

    def _fake_load(path: str, sr: int, mono: bool, offset: float, duration):
        return [0.0] * 50, 48_000

    _install_librosa_stub(_fake_load)

    def _fake_sf_write(name: str, y: Any, sr: int) -> None:
        with open(name, "wb") as f:
            f.write(b"x")

    _install_soundfile_stub(_fake_sf_write)

    # Source path doesn't exist; unlink raises OSError, which is logged and tolerated.
    with caplog.at_level("WARNING", logger="audio_graphy.services.campplus_service"):
        out_path = svc._crop_audio("/nonexistent/src.wav", 0.0, None)
        try:
            assert os.path.exists(out_path)
            assert any(
                "Temporary audio cleanup failed" in record.message
                and "/nonexistent/src.wav" in record.message
                for record in caplog.records
            )
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)
