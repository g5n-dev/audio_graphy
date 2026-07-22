"""Extra coverage tests for ``audio_graphy.services.campplus_service`` helpers.

Targets the pure-Python helper functions which don't need funasr / librosa:
- ``_save_tmp`` writes bytes to disk and returns the path.
- ``_diarize_with_sv_only`` returns whole-file single-speaker timeline when
  duration >= min_segment_sec; empty list otherwise.
- ``_diarize_with_diarize_model`` parses funasr ``sentence_info`` payload
  and skips too-short segments.
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
# _diarize_with_sv_only
# ============================================================


def test_diarize_sv_only_short_audio_returns_empty() -> None:
    """Audio shorter than min_segment_sec returns empty segment list."""

    def _fake_load(path: str, sr: int, mono: bool) -> tuple[list[int], int]:
        return [0] * 100, 16_000  # 100 samples @ 16kHz → 6.25ms

    _install_librosa_stub(_fake_load)

    segs, dur = svc._diarize_with_sv_only("/fake", min_segment_sec=0.5)
    assert segs == []
    assert dur > 0


def test_diarize_sv_only_long_audio_returns_single_segment() -> None:
    """Audio >= min_segment_sec returns a single whole-file segment."""

    def _fake_load(path: str, sr: int, mono: bool) -> tuple[list[int], int]:
        return [0] * 48_000, 48_000  # 1.0s @ 48kHz

    _install_librosa_stub(_fake_load)

    segs, dur = svc._diarize_with_sv_only("/fake", min_segment_sec=0.5)
    assert len(segs) == 1
    assert segs[0]["speaker_id"] == "spk_0"
    assert segs[0]["start_sec"] == 0.0
    assert segs[0]["end_sec"] == dur
    assert dur == pytest.approx(1.0, abs=1e-6)


# ============================================================
# _diarize_with_diarize_model
# ============================================================


def test_diarize_model_empty_response_returns_empty() -> None:
    svc._DIARIZE_MODEL = types.SimpleNamespace(generate=lambda **kw: [])
    segs, dur = svc._diarize_with_diarize_model(
        "/fake", min_segment_sec=0.5, max_speakers=5
    )
    assert segs == []
    assert dur == 0.0


def test_diarize_model_non_list_response_returns_empty() -> None:
    svc._DIARIZE_MODEL = types.SimpleNamespace(
        generate=lambda **kw: "not-a-list"
    )
    segs, _ = svc._diarize_with_diarize_model(
        "/fake", min_segment_sec=0.5, max_speakers=5
    )
    assert segs == []


def test_diarize_model_missing_sentence_info_returns_empty() -> None:
    svc._DIARIZE_MODEL = types.SimpleNamespace(
        generate=lambda **kw: [{"no_sentence_info": True}]
    )
    segs, _ = svc._diarize_with_diarize_model(
        "/fake", min_segment_sec=0.5, max_speakers=5
    )
    assert segs == []


def test_diarize_model_parses_sentence_info_and_drops_short() -> None:
    """Valid sentence_info is parsed; segments shorter than min are dropped."""

    def _fake_load(path: str, sr: int, mono: bool) -> tuple[list[int], int]:
        return [0] * 48_000, 48_000  # 1.0s @ 48kHz

    _install_librosa_stub(_fake_load)

    sentence_info = [
        {"start": 0.0, "end": 0.8, "spk_label": 0},   # 0.8s ≥ 0.5 → kept
        {"start": 0.8, "end": 0.9, "spk_label": 1},   # 0.1s < 0.5 → dropped
        {"start": 0.9, "end": 1.0, "spk_label": 0},   # 0.1s < 0.5 → dropped
    ]
    svc._DIARIZE_MODEL = types.SimpleNamespace(
        generate=lambda **kw: [{"sentence_info": sentence_info}]
    )

    segs, dur = svc._diarize_with_diarize_model(
        "/fake", min_segment_sec=0.5, max_speakers=5
    )
    assert dur == pytest.approx(1.0, abs=1e-6)
    assert len(segs) == 1
    assert segs[0]["speaker_id"] == "spk_0"
    assert segs[0]["start_sec"] == 0.0
    assert segs[0]["end_sec"] == 0.8


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


def test_crop_audio_handles_unlink_failure(tmp_path) -> None:
    """If source unlink fails, crop still returns the new path (OSError swallowed)."""

    def _fake_load(path: str, sr: int, mono: bool, offset: float, duration):
        return [0.0] * 50, 48_000

    _install_librosa_stub(_fake_load)

    def _fake_sf_write(name: str, y: Any, sr: int) -> None:
        with open(name, "wb") as f:
            f.write(b"x")

    _install_soundfile_stub(_fake_sf_write)

    # Source path doesn't exist; unlink will raise OSError but be swallowed.
    out_path = svc._crop_audio("/nonexistent/src.wav", 0.0, None)
    try:
        assert os.path.exists(out_path)
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)
