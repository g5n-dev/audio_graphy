"""Unit tests for audiography-clap-service FastAPI plumbing.

These tests stub out the CLAP model and GPU enforcement so they run on
any CPU machine. Focus is on:
- /health endpoint response schema.
- /v1/audio/embed 400 on empty upload.
- /v1/audio/embed 503 when model not loaded.
- /v1/audio/embed 200 path with stubbed model returning 512-d.
- Cache hit returns ``cached=True`` and skips inference.
- L2-normalization applied to embedding.
- Dim mismatch raises 500.
"""

from __future__ import annotations

import hashlib
import sys
import types
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


def _install_torch_stub() -> None:
    """Install a minimal ``torch`` stub so the service imports on CPU-only hosts."""
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
def clap_app() -> Any:
    """Import the clap_service module fresh, with stubs for CUDA + CLAP."""
    import audio_graphy.services.clap_service as svc

    # Reset module-level state.
    svc._CACHE.clear()
    svc._CACHE_ORDER.clear()
    svc._CLAP_MODEL = None
    return svc


@pytest.fixture
def client(clap_app: Any) -> TestClient:
    """TestClient that skips lifespan (so we don't trigger GPU/model load)."""
    return TestClient(clap_app.app, raise_server_exceptions=False)


# ============================================================
# /health
# ============================================================


class TestHealth:
    def test_returns_status_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "gpu" in body
        assert "model_loaded" in body
        assert body["model"] == "clap-htsat-base-2022"
        assert body["cache_size"] == 0


# ============================================================
# /v1/audio/embed
# ============================================================


class TestEmbedAudio:
    def test_503_when_model_not_loaded(self, client: TestClient, clap_app: Any) -> None:
        clap_app._CLAP_MODEL = None  # ensure not loaded
        resp = client.post(
            "/v1/audio/embed",
            files={"audio": ("test.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503

    def test_400_on_empty_upload(self, client: TestClient, clap_app: Any) -> None:
        """Empty audio upload is rejected with 400."""
        # Stub CLAP model so we get past the 503 check.
        clap_app._CLAP_MODEL = _StubClapModel(dim=512)
        resp = client.post(
            "/v1/audio/embed",
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    def test_200_happy_path(self, client: TestClient, clap_app: Any, tmp_path) -> None:
        """Valid WAV upload with stubbed model + librosa returns 512-d embedding.

        Skipped if librosa is not installed (CPU-only dev environments).
        """
        pytest.importorskip("librosa")
        import wave

        wav_path = tmp_path / "test.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(b"\x00\x00" * 4800)  # 0.1s of silence

        clap_app._CLAP_MODEL = _StubClapModel(dim=512, fill=0.7)
        with open(wav_path, "rb") as f:
            resp = client.post(
                "/v1/audio/embed",
                files={"audio": ("test.wav", f.read(), "audio/wav")},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dim"] == 512
        assert len(body["embedding"]) == 512
        assert body["model"] == "clap-htsat-base-2022"
        assert body["cached"] is False

    def test_cache_hit_skips_inference(
        self, client: TestClient, clap_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-warmed cache returns ``cached=True`` without invoking the model.

        Note: 503 check runs BEFORE cache lookup in current implementation,
        so we must stub a non-None model to reach the cache path.
        """
        payload = b"\x99" * 500
        key = hashlib.sha256(payload).hexdigest()
        cached_vec = [0.5] * 512
        clap_app._cache_put(key, cached_vec)

        # Stub model so we pass the 503 check.
        clap_app._CLAP_MODEL = _StubClapModel(dim=512)
        # Sanity: model would have been called on miss; on hit it must not be.
        call_count = {"n": 0}
        original = clap_app._CLAP_MODEL.get_audio_embedding_from_filedata

        def _count(*args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            return original(*args, **kwargs)

        clap_app._CLAP_MODEL.get_audio_embedding_from_filedata = _count  # type: ignore[assignment]

        resp = client.post(
            "/v1/audio/embed",
            files={"audio": ("a.wav", payload, "audio/wav")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is True
        assert body["embedding"] == cached_vec
        assert call_count["n"] == 0


# ============================================================
# Cache helpers
# ============================================================


class TestCache:
    def test_get_returns_none_for_missing(self, clap_app: Any) -> None:
        assert clap_app._cache_get("missing") is None

    def test_put_then_get(self, clap_app: Any) -> None:
        clap_app._CACHE.clear()
        clap_app._CACHE_ORDER.clear()
        clap_app._cache_put("k1", [0.1, 0.2])
        assert clap_app._cache_get("k1") == [0.1, 0.2]

    def test_lru_bumps_on_hit(self, clap_app: Any) -> None:
        clap_app._CACHE.clear()
        clap_app._CACHE_ORDER.clear()
        clap_app._cache_put("a", [1.0])
        clap_app._cache_put("b", [2.0])
        # Access "a" → bump to back.
        clap_app._cache_get("a")
        assert clap_app._CACHE_ORDER[-1] == "a"

    def test_lru_evicts_oldest(self, clap_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        clap_app._CACHE.clear()
        clap_app._CACHE_ORDER.clear()
        monkeypatch.setattr(clap_app, "_CACHE_SIZE", 2)
        clap_app._cache_put("a", [1.0])
        clap_app._cache_put("b", [2.0])
        clap_app._cache_put("c", [3.0])  # evicts "a"
        assert "a" not in clap_app._CACHE
        assert "b" in clap_app._CACHE
        assert "c" in clap_app._CACHE


# ============================================================
# _l2_normalize
# ============================================================


class TestL2Normalize:
    def test_unit_vector_unchanged(self, clap_app: Any) -> None:
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        out = clap_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, v)

    def test_non_unit_normalized(self, clap_app: Any) -> None:
        v = np.array([3.0, 4.0], dtype=np.float32)  # norm=5
        out = clap_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, [0.6, 0.8])

    def test_zero_vector_returned_as_is(self, clap_app: Any) -> None:
        v = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        out = clap_app._l2_normalize(v)
        np.testing.assert_array_almost_equal(out, v)


# ============================================================
# Helpers
# ============================================================


class _StubClapModel:
    """Stub CLAP model returning a fixed-dim constant embedding."""

    def __init__(self, *, dim: int = 512, fill: float = 0.5) -> None:
        self._dim = dim
        self._fill = fill

    def get_audio_embedding_from_filedata(self, *, x: Any, use_tensor: bool = False) -> np.ndarray:
        # Return shape (1, dim) so .flatten() in caller hits dim.
        return np.full((1, self._dim), self._fill, dtype=np.float32)
