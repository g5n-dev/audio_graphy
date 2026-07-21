"""Unit tests for audiography-campplus-service FastAPI plumbing.

Stubs out funasr AutoModel + librosa so tests run anywhere. Focus is on:
- /health endpoint response schema.
- /v1/diarize 400 / 503 error paths + 200 with SV-only fallback.
- /v1/voiceprint/extract 200 + 400 + 503 + dim mismatch.
- _l2_normalize helper.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


def _install_torch_stub() -> None:
    """Stub torch so campplus_service imports cleanly on CPU hosts."""
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


@pytest.fixture
def campplus_app() -> Any:
    """Import campplus_service fresh; reset module state."""
    import audio_graphy.services.campplus_service as svc

    svc._SV_MODEL = None
    svc._DIARIZE_MODEL = None
    return svc


@pytest.fixture
def client(campplus_app: Any) -> TestClient:
    """TestClient skipping lifespan (no funasr load)."""
    return TestClient(campplus_app.app, raise_server_exceptions=False)


class _StubSVModel:
    """Stub CAM++ SV model returning a fixed-dim embedding."""

    def __init__(self, *, dim: int = 192, fill: float = 0.5) -> None:
        self._dim = dim
        self._fill = fill
        self.generate_calls = 0

    def generate(
        self,
        *,
        input: str,
        cache: dict | None = None,
        language: str = "zh-cn",
    ) -> list[dict]:
        self.generate_calls += 1
        return [{"spk_embedding": [self._fill] * self._dim}]


# ============================================================
# /health
# ============================================================


class TestHealth:
    def test_returns_status_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "sv_loaded" in body
        assert "device" in body
        assert body["sv_model"] == "iic/speech_campplus_sv_zh-cn_16k-common"
        assert body["dim"] == 192

    def test_model_not_loaded_reflected_in_health(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = None
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["sv_loaded"] is False


# ============================================================
# /v1/diarize
# ============================================================


class TestDiarize:
    def test_503_when_model_not_loaded(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = None
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503

    def test_400_on_empty_upload(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = _StubSVModel()
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    def test_200_with_sv_only_fallback(
        self, client: TestClient, campplus_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When diarize model is None, SV-only fallback returns 1-speaker timeline."""
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._DIARIZE_MODEL = None

        # _diarize_with_sv_only is a sync function.
        def _fake_sv_only(
            path: str, min_seg: float
        ) -> tuple[list[dict[str, Any]], float]:
            return (
                [{"speaker_id": "spk_0", "start_sec": 0.0, "end_sec": 5.0}],
                5.0,
            )

        monkeypatch.setattr(
            campplus_app, "_diarize_with_sv_only", _fake_sv_only
        )

        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["num_speakers"] == 1
        assert len(body["segments"]) == 1
        assert body["segments"][0]["speaker_id"] == "spk_0"


# ============================================================
# /v1/voiceprint/extract
# ============================================================


class TestExtractVoiceprint:
    def test_503_when_model_not_loaded(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = None
        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503

    def test_400_on_empty_upload(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = _StubSVModel()
        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    def test_dim_mismatch_returns_500(
        self,
        client: TestClient,
        campplus_app: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When SV model returns wrong-dim vector → 500."""
        campplus_app._SV_MODEL = _StubSVModel(dim=128)  # wrong dim
        # Stub _crop_audio to be a no-op (avoids librosa).
        monkeypatch.setattr(
            campplus_app, "_crop_audio", lambda path, s, e: path
        )
        # Stub _save_tmp to skip file write.
        monkeypatch.setattr(
            campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav"
        )
        # Stub os.unlink to no-op.
        import os

        monkeypatch.setattr(os, "unlink", lambda *a, **kw: None)

        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
        )
        assert resp.status_code == 500
        assert "dim mismatch" in resp.json()["detail"].lower()

    def test_200_happy_path(
        self,
        client: TestClient,
        campplus_app: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stubbed happy path returns 192-d L2-normalized vector."""
        campplus_app._SV_MODEL = _StubSVModel(dim=192, fill=0.7)
        monkeypatch.setattr(
            campplus_app, "_crop_audio", lambda path, s, e: path
        )
        monkeypatch.setattr(
            campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav"
        )
        import os

        monkeypatch.setattr(os, "unlink", lambda *a, **kw: None)

        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
            data={"speaker_id": "spk_0", "start_sec": "0.0", "end_sec": "5.0"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dim"] == 192
        assert len(body["voiceprint"]) == 192
        # L2 normalized: |v| ≈ 1
        norm = sum(x * x for x in body["voiceprint"]) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-5)


# ============================================================
# _l2_normalize
# ============================================================


class TestL2Normalize:
    def test_unit_vector_unchanged(self, campplus_app: Any) -> None:
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        out = campplus_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, v)

    def test_non_unit_normalized(self, campplus_app: Any) -> None:
        v = np.array([3.0, 4.0], dtype=np.float32)  # norm=5
        out = campplus_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, [0.6, 0.8])

    def test_zero_vector_returned_as_is(self, campplus_app: Any) -> None:
        v = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        out = campplus_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, v)
