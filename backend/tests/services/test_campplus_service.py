"""Unit tests for audiography-campplus-service FastAPI plumbing.

Stubs out funasr AutoModel + librosa so tests run anywhere. Focus is on:
- /health reporting each model's load state honestly.
- /v1/diarize 400 / 503 error paths + 200 off the speaker-labelled pipeline.
- /v1/audio/transcriptions 400 / 503 + 200 verbose_json.
- /v1/voiceprint/extract 200 + 400 + 503 + dim mismatch, and its independence
  from the ASR pipeline.
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
    svc._ASR_MODEL = None
    svc._ASR_LOAD_ERROR = None
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


class _StubASRPipeline:
    """Stub funasr AutoModel with a speaker model attached.

    ``spk_model`` is what the service reads to decide whether diarization is
    genuinely available, so it is a real attribute here rather than implied.
    """

    def __init__(self, payload: Any = None, *, spk: bool = True) -> None:
        self.spk_model = object() if spk else None
        self.cb_model = types.SimpleNamespace(
            spectral_cluster=types.SimpleNamespace(max_num_spks=15)
        )
        self.generate_calls = 0
        self._payload = (
            payload
            if payload is not None
            else [
                {
                    "text": "今 天 好。 明 天 也 好。",
                    "sentence_info": [
                        {"text": "今 天 好。", "start": 0, "end": 5000, "spk": 0},
                        {"text": "明 天 也 好。", "start": 5200, "end": 9000, "spk": 1},
                    ],
                }
            ]
        )

    def generate(self, **kwargs: Any) -> Any:
        self.generate_calls += 1
        return self._payload


@pytest.fixture
def stub_audio_io(campplus_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tmp-file and librosa work out of the FastAPI tests."""
    monkeypatch.setattr(campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav")
    monkeypatch.setattr(campplus_app, "_unlink_tmp", lambda path: None)
    monkeypatch.setattr(campplus_app, "_audio_duration_sec", lambda path: 9.5)


# ============================================================
# /health
# ============================================================


class TestHealth:
    def test_reports_each_model_separately(self, client: TestClient, campplus_app: Any) -> None:
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = _StubASRPipeline()
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["sv_loaded"] is True
        assert body["asr_loaded"] is True
        assert body["spk_loaded"] is True
        assert body["diarize_loaded"] is True
        assert body["asr_error"] is None
        assert body["sv_model"] == "iic/speech_campplus_sv_zh-cn_16k-common"
        assert body["dim"] == 192

    def test_not_ok_while_asr_still_loading(self, client: TestClient, campplus_app: Any) -> None:
        """SV up, ASR not yet: reporting "ok" here would be a lie."""
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = None
        body = client.get("/health").json()
        assert body["status"] == "loading"
        assert body["sv_loaded"] is True
        assert body["asr_loaded"] is False
        assert body["spk_loaded"] is False

    def test_degraded_and_never_ok_when_asr_load_failed(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = None
        campplus_app._ASR_LOAD_ERROR = "RuntimeError: no space left on device"
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["asr_loaded"] is False
        assert body["asr_error"] == "RuntimeError: no space left on device"

    def test_spk_not_claimed_when_pipeline_has_no_speaker_model(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        """An ASR pipeline without spk must not advertise diarization.

        And it must read "degraded", not "loading": the pipeline is built, so
        nothing is in flight and this state never resolves — /v1/diarize 503s
        for the life of the process. "loading" would hide exactly the failure
        ``spk_loaded`` was added to expose.
        """
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = _StubASRPipeline(spk=False)
        body = client.get("/health").json()
        assert body["asr_loaded"] is True
        assert body["spk_loaded"] is False
        assert body["diarize_loaded"] is False
        assert body["status"] == "degraded"

    def test_model_not_loaded_reflected_in_health(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = None
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["sv_loaded"] is False
        assert resp.json()["status"] == "error"


# ============================================================
# /v1/diarize
# ============================================================


class TestDiarize:
    def test_503_when_asr_pipeline_unavailable(
        self, client: TestClient, campplus_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No fabricated single-speaker timeline — an honest 503 instead."""
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = None

        def _boom() -> Any:
            raise RuntimeError("model download failed")

        monkeypatch.setattr(campplus_app, "_build_asr_model", _boom)
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503
        assert "model download failed" in resp.json()["detail"]

    def test_503_immediately_while_a_load_is_in_flight(
        self, client: TestClient, campplus_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requests are not queued behind a ~120s load they would outlive."""
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = None

        def _never_called() -> Any:  # pragma: no cover - must not run
            raise AssertionError("a second pipeline load was started")

        monkeypatch.setattr(campplus_app, "_build_asr_model", _never_called)
        # Stand in for the startup preload holding the lock.
        monkeypatch.setattr(campplus_app._ASR_LOAD_LOCK, "locked", lambda: True)

        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503
        assert "still loading" in resp.json()["detail"]

    def test_503_when_pipeline_has_no_speaker_model(
        self, client: TestClient, campplus_app: Any
    ) -> None:
        campplus_app._SV_MODEL = _StubSVModel()
        campplus_app._ASR_MODEL = _StubASRPipeline(spk=False)
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503
        assert "speaker model" in resp.json()["detail"]

    def test_400_on_empty_upload(self, client: TestClient, campplus_app: Any) -> None:
        campplus_app._ASR_MODEL = _StubASRPipeline()
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    def test_200_timeline_from_speaker_labels(
        self, client: TestClient, campplus_app: Any, stub_audio_io: None
    ) -> None:
        """Response keeps the shape voiceprint_cam.py parses, in seconds."""
        pipeline = _StubASRPipeline()
        campplus_app._ASR_MODEL = pipeline

        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
            data={"min_segment_sec": "0.5", "max_speakers": "4"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["num_speakers"] == 2
        assert body["duration_sec"] == pytest.approx(9.5)
        assert body["model"] == "iic/speech_campplus_sv_zh-cn_16k-common"
        assert [s["speaker_id"] for s in body["segments"]] == ["spk_0", "spk_1"]
        # ms → seconds, and inside the recording.
        assert body["segments"][0]["end_sec"] == pytest.approx(5.0)
        assert body["segments"][1]["start_sec"] == pytest.approx(5.2)
        assert all(s["confidence"] is None for s in body["segments"])
        # max_speakers reached the clusterer, not funasr's oracle knob.
        assert pipeline.cb_model.spectral_cluster.max_num_spks == 4

    def test_500_when_speaker_labels_missing(
        self, client: TestClient, campplus_app: Any, stub_audio_io: None
    ) -> None:
        campplus_app._ASR_MODEL = _StubASRPipeline(
            [{"sentence_info": [{"start": 0, "end": 5000, "text": "hi"}]}]
        )
        resp = client.post(
            "/v1/diarize",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
        )
        assert resp.status_code == 500
        assert "spk" in resp.json()["detail"]


# ============================================================
# /v1/audio/transcriptions
# ============================================================


class TestTranscriptions:
    def test_400_on_empty_upload(self, client: TestClient, campplus_app: Any) -> None:
        campplus_app._ASR_MODEL = _StubASRPipeline()
        resp = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("empty.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    def test_503_when_pipeline_unavailable(
        self, client: TestClient, campplus_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campplus_app._ASR_MODEL = None
        monkeypatch.setattr(
            campplus_app,
            "_build_asr_model",
            lambda: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        resp = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503

    def test_200_verbose_json_matches_adapter_contract(
        self, client: TestClient, campplus_app: Any, stub_audio_io: None
    ) -> None:
        """Exactly the fields FunASRAdapter reads, with seconds and no fake confidence."""
        campplus_app._ASR_MODEL = _StubASRPipeline()
        resp = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"\x00" * 1000, "audio/wav")},
            data={
                "model": "fun-asr-nano",
                "response_format": "verbose_json",
                "language": "zh",
                "temperature": "0.0",
                "timestamp_granularities[]": "segment",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == "今天好。明天也好。"
        assert body["language"] == "zh"
        assert body["model"] == "fun-asr-nano"
        assert body["duration"] == pytest.approx(9.5)
        assert [s["id"] for s in body["segments"]] == [0, 1]
        assert body["segments"][0]["start"] == pytest.approx(0.0)
        assert body["segments"][0]["end"] == pytest.approx(5.0)
        assert body["segments"][0]["text"] == "今天好。"
        assert all("confidence" not in s for s in body["segments"])

    def test_response_format_text_returns_plain_body(
        self, client: TestClient, campplus_app: Any, stub_audio_io: None
    ) -> None:
        campplus_app._ASR_MODEL = _StubASRPipeline()
        resp = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"\x00" * 1000, "audio/wav")},
            data={"response_format": "text"},
        )
        assert resp.status_code == 200
        assert resp.text == "今天好。明天也好。"


# ============================================================
# /v1/models
# ============================================================


def test_list_models_reports_served_name(client: TestClient) -> None:
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "fun-asr-nano"


# ============================================================
# /v1/voiceprint/extract
# ============================================================


class TestExtractVoiceprint:
    def test_503_when_model_not_loaded(self, client: TestClient, campplus_app: Any) -> None:
        campplus_app._SV_MODEL = None
        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"\x00" * 100, "audio/wav")},
        )
        assert resp.status_code == 503

    def test_400_on_empty_upload(self, client: TestClient, campplus_app: Any) -> None:
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
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)
        # Stub _save_tmp to skip file write.
        monkeypatch.setattr(campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav")
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
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)
        monkeypatch.setattr(campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav")
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

    def test_survives_a_failed_asr_pipeline(
        self,
        client: TestClient,
        campplus_app: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The heavy pipeline failing must not take voiceprint extraction down."""
        campplus_app._SV_MODEL = _StubSVModel(dim=192, fill=0.7)
        campplus_app._ASR_MODEL = None
        campplus_app._ASR_LOAD_ERROR = "OSError: [Errno 28] No space left on device"
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)
        monkeypatch.setattr(campplus_app, "_save_tmp", lambda data, suffix: "/tmp/fake.wav")
        monkeypatch.setattr(campplus_app, "_unlink_tmp", lambda path: None)

        resp = client.post(
            "/v1/voiceprint/extract",
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["voiceprint"]) == 192
        # And the failure is still visible rather than papered over.
        assert client.get("/health").json()["status"] == "degraded"


# ============================================================
# Inference slot isolation
# ============================================================


class TestInferenceSlotIsolation:
    """The ASR pipeline and the SV model must not share one inference slot.

    Since /v1/diarize started running the full Paraformer pass (RTF ~0.2), a
    shared slot meant a long recording held it for minutes while every
    /v1/voiceprint/extract queued behind it blew CAMPlusPlusAdapter's 60 s
    timeout — and chunker.py swallows that into speaker=None for the whole
    recording, so the operator sees no error, just a file with no speakers.

    Asserted by holding a slot rather than by measuring latency: a timing
    assertion would go flaky on a loaded box, and the reason this survived the
    happy-path suite is that status codes alone never showed it.
    """

    def test_asr_and_sv_slots_are_distinct(self, campplus_app: Any) -> None:
        assert campplus_app._ASR_SEMAPHORE is not campplus_app._SV_SEMAPHORE

    async def test_voiceprint_serves_while_the_asr_slot_is_held(
        self,
        campplus_app: Any,
        stub_audio_io: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An in-flight ASR pass must not stall voiceprint extraction."""
        import asyncio

        import httpx

        campplus_app._SV_MODEL = _StubSVModel(dim=192, fill=0.7)
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)

        transport = httpx.ASGITransport(app=campplus_app.app)
        async with campplus_app._ASR_SEMAPHORE:  # an ASR pass is in flight
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                # Shared slots would deadlock here instead of returning.
                resp = await asyncio.wait_for(
                    ac.post(
                        "/v1/voiceprint/extract",
                        files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
                    ),
                    timeout=10.0,
                )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dim"] == 192

    async def test_sv_slot_still_serializes_voiceprint(
        self,
        campplus_app: Any,
        stub_audio_io: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Splitting the slots must not make SV inference concurrent."""
        import asyncio

        import httpx

        campplus_app._SV_MODEL = _StubSVModel(dim=192, fill=0.7)
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)

        transport = httpx.ASGITransport(app=campplus_app.app)
        async with campplus_app._SV_SEMAPHORE:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        ac.post(
                            "/v1/voiceprint/extract",
                            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
                        ),
                        timeout=0.5,
                    )

    def test_duration_sec_reports_the_audio_actually_used(
        self,
        client: TestClient,
        campplus_app: Any,
        stub_audio_io: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """protocols.py calls duration_sec a quality signal; 0.0 is not one.

        It was hardcoded, so every VoiceprintResult carried a duration of zero
        and any duration-based gating downstream silently saw "no signal".
        """
        campplus_app._SV_MODEL = _StubSVModel(dim=192, fill=0.7)
        monkeypatch.setattr(campplus_app, "_crop_audio", lambda path, s, e: path)
        monkeypatch.setattr(campplus_app, "_audio_duration_sec", lambda path: 4.25)

        resp = client.post(
            "/v1/voiceprint/extract",
            data={"start_sec": "1.0", "end_sec": "5.25"},
            files={"audio": ("a.wav", b"\x00" * 1000, "audio/wav")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["duration_sec"] == pytest.approx(4.25)


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
