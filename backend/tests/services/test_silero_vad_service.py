"""Batch VAD service — the segmentation rules, and the contract it must satisfy.

Two classes of test here. The merging rules are pure and get exercised against
synthetic probability sequences. The rest pin the seams that would otherwise
fail only in production: the adapter's wire contract, and the constants shared
with the streaming path.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest

from audio_graphy.adapters.real import streaming_vad_silero
from audio_graphy.services import silero_vad_service as svc

pytestmark = pytest.mark.unit


async def _async_return_inner(value):
    return value


def _async_return(value):
    """A coroutine-returning stand-in for an async function."""
    return _async_return_inner(value)


_WINDOW = svc.SILERO_CHUNK_SEC  # 0.032s


def _windows(seconds: float) -> int:
    return round(seconds / _WINDOW)


def _probs(*runs: tuple[float, float]) -> list[float]:
    """Build a probability sequence from (probability, duration_sec) runs."""
    out: list[float] = []
    for value, seconds in runs:
        out.extend([value] * _windows(seconds))
    return out


class TestSegmentMerging:
    def test_a_blip_shorter_than_the_floor_is_dropped(self) -> None:
        """A 0.2s spike is noise; embedding it wastes a provider call on nothing."""
        probs = _probs((0.0, 1.0), (0.9, 0.2), (0.0, 1.0))

        spans = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        assert spans == []

    def test_a_within_sentence_pause_does_not_split_the_utterance(self) -> None:
        """Sub-threshold silence must not shatter one utterance into fragments.

        A 0.2s breath is well under MIN_SILENCE_SEC (0.35), so the two speech
        runs either side of it are one segment — not two that ASR would then
        transcribe without each other's context.
        """
        probs = _probs((0.9, 2.0), (0.0, 0.2), (0.9, 2.0))

        spans = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        # One span, not two. It covers the whole clip: the leading pad clamps to
        # the file start and the trailing pad clamps to the file end, so the span
        # is exactly the input duration.
        assert len(spans) == 1
        assert spans[0].start_sec == 0.0
        assert spans[0].end_sec == pytest.approx(len(probs) * _WINDOW, abs=0.01)

    def test_a_real_silence_does_split(self) -> None:
        """Above MIN_SILENCE_SEC the gap is a genuine boundary."""
        probs = _probs((0.9, 2.0), (0.0, 1.0), (0.9, 2.0))

        spans = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        assert len(spans) == 2

    def test_an_unbroken_monologue_is_split_at_the_ceiling(self) -> None:
        """ASR has a per-request ceiling; a 90s run without pauses must not exceed it."""
        probs = _probs((0.9, 90.0))

        spans = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        assert len(spans) >= 3
        assert all(span.end_sec - span.start_sec <= 30.0 + 1e-6 for span in spans)

    def test_speech_running_to_the_end_of_the_file_is_kept(self) -> None:
        """No trailing silence to close the segment — it must still be emitted."""
        probs = _probs((0.0, 0.5), (0.9, 3.0))

        spans = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        assert len(spans) == 1
        assert spans[0].end_sec == pytest.approx(len(probs) * _WINDOW, abs=0.05)

    def test_confidence_is_the_mean_over_the_speech_windows(self) -> None:
        probs = _probs((0.6, 1.0), (1.0, 1.0))

        [span] = svc.spans_from_probabilities(probs, min_segment_sec=0.5, max_segment_sec=30.0)

        assert span.confidence == pytest.approx(0.8, abs=0.02)


class TestSharedWithTheStreamingPath:
    """The two paths must segment the same audio identically.

    Batch and streaming are separate call sites for the same model. If their
    framing diverges, one recording gets different boundaries depending on
    whether it arrived as a file or over a websocket — and nobody notices until
    two runs of the same audio disagree.
    """

    def test_sample_rate_and_window_are_the_streaming_module_s(self) -> None:
        assert svc.SILERO_SAMPLE_RATE is streaming_vad_silero.SILERO_SAMPLE_RATE
        assert svc.SILERO_CHUNK_SAMPLES is streaming_vad_silero.SILERO_CHUNK_SAMPLES
        assert svc.SILERO_CHUNK_SEC == streaming_vad_silero.SILERO_CHUNK_SEC

    def test_the_thresholds_are_deliberately_independent(self) -> None:
        """Documented divergence, asserted so it stays a decision and not a drift.

        Streaming opens early and closes late to avoid clipping a live
        utterance; batch sees the whole waveform and uses one symmetric
        threshold. If someone ever unifies them, this test is where they say so.
        """
        assert svc.SPEECH_THRESHOLD == 0.5
        assert svc.MIN_SILENCE_SEC == 0.35


class _StubSession:
    """Minimal ONNX stand-in: one probability per window, community I/O names."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = probs
        self._calls = 0

    def get_inputs(self) -> list[Any]:
        return [type("M", (), {"name": name})() for name in ("input", "h", "c")]

    def run(self, _outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        import numpy as np

        value = self._probs[self._calls] if self._calls < len(self._probs) else 0.0
        self._calls += 1
        return [
            np.array([[value]], dtype=np.float32),
            feeds["h"],
            feeds["c"],
        ]


def test_probabilities_pads_the_trailing_partial_window() -> None:
    """A tail shorter than one window must not be silently discarded."""
    import numpy as np

    session = _StubSession([0.9] * 10)
    # 2.5 windows' worth of samples.
    pcm = np.zeros(svc.SILERO_CHUNK_SAMPLES * 2 + 100, dtype=np.int16).tobytes()

    probs = svc.probabilities(session, pcm)

    assert len(probs) == 3, "two full windows plus the zero-padded remainder"


class TestAdapterContract:
    """The wire shape is the adapter's, and it predates this service.

    ``SileroVADAdapter`` sends the file under the part name ``audio`` and parses
    ``segments[].start_sec/end_sec/confidence``. A rename on either side fails
    only in production, where the service's own unit tests would still pass.
    """

    async def test_the_real_adapter_round_trips_through_the_real_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """The adapter's actual multipart hits the actual route over ASGI.

        This replaces two source-text greps that certified nothing: renaming
        the service's ``start_sec`` to ``start`` left them green because the
        adapter's own fixtures hardcoded the old keys. Here the request is
        built by ``SileroVADAdapter.segment`` and parsed by
        ``_parse_segments`` — a rename on either side of the wire fails this
        test, not production.
        """
        from pathlib import Path

        import httpx

        from audio_graphy.adapters.real.vad_silero import SileroVADAdapter

        monkeypatch.setattr(svc, "_session", object())
        monkeypatch.setattr(
            svc,
            "_decode_to_pcm16",
            lambda raw, suffix: _async_return(b"\x00\x00" * svc.SILERO_CHUNK_SAMPLES),
        )
        # 40 confident windows = 1.28s of speech: one span, confidence 0.9.
        monkeypatch.setattr(svc, "probabilities", lambda session, pcm, deadline=None: [0.9] * 40)

        wav = Path(str(tmp_path)) / "a.wav"
        wav.write_bytes(b"RIFF-ish bytes; the decoder is patched out")

        adapter = SileroVADAdapter(url="http://vad")
        adapter._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=svc.app), base_url="http://vad"
        )
        try:
            segments = await adapter.segment(str(wav), min_segment_sec=0.5, max_segment_sec=30.0)
        finally:
            await adapter.aclose()

        assert len(segments) == 1
        assert segments[0].start_sec == 0.0
        assert segments[0].end_sec == pytest.approx(1.28, abs=0.01)
        assert segments[0].confidence == pytest.approx(0.9, abs=0.01)

    def test_the_adapter_timeout_bounds_the_service_timeout(self) -> None:
        """The service must give up before the client does.

        Otherwise the adapter times out at 30s while the service keeps the
        single concurrency slot for the rest of the run, and the retry queues
        behind work nobody is waiting for.
        """
        from audio_graphy.adapters.real import vad_silero

        assert svc._INFER_TIMEOUT_SEC < vad_silero._DEFAULT_TIMEOUT


class TestDegradesHonestlyWithoutTheModel:
    """The operator supplies the ONNX file; a missing one must say so.

    This is the state a fresh deployment is in before anyone runs the documented
    curl. Silently returning zero segments would look like "this recording has
    no speech" — indistinguishable from a real answer, and the pipeline would
    happily index an empty transcript.
    """

    async def test_health_reports_degraded_and_names_the_missing_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI

        # The exact state of a deployment that skipped the download. The path
        # check runs before the onnxruntime import, so this exercises the real
        # branch even in this venv, where onnxruntime is absent — previously
        # every path failed identically at the import and /etc/hosts produced
        # the same "coverage" as a genuinely missing file.
        monkeypatch.setenv("SILERO_VAD_MODEL_PATH", "/definitely/not/here.onnx")
        async with svc.lifespan(FastAPI()):
            health = await svc.health()

        assert health["status"] == "degraded"
        assert health["model_loaded"] is False
        assert "/definitely/not/here.onnx" in (health["error"] or ""), (
            "the error must name the path the operator needs to fill"
        )
        assert "SILERO_VAD_MODEL_FILE" in (health["error"] or ""), "and the variable that fills it"

    async def test_health_reports_ok_once_a_session_loads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """Positive control: without it, the degraded assertions above would
        stay green even if lifespan failed unconditionally."""

        import sys
        import types
        from pathlib import Path

        model = Path(str(tmp_path)) / "model.onnx"
        model.write_bytes(b"weights")
        monkeypatch.setenv("SILERO_VAD_MODEL_PATH", str(model))

        fake = types.ModuleType("onnxruntime")
        fake.SessionOptions = lambda: types.SimpleNamespace(
            intra_op_num_threads=0, inter_op_num_threads=0
        )
        fake.InferenceSession = lambda *a, **k: object()
        monkeypatch.setitem(sys.modules, "onnxruntime", fake)

        from fastapi import FastAPI

        async with svc.lifespan(FastAPI()):
            health = await svc.health()

        assert health["status"] == "ok"
        assert health["model_loaded"] is True
        assert health["error"] is None

    async def test_requests_are_refused_with_503_not_an_empty_answer(self) -> None:
        from fastapi import FastAPI, HTTPException

        async with svc.lifespan(FastAPI()):
            with pytest.raises(HTTPException) as caught:
                svc._require_session()

        assert caught.value.status_code == 503
        # The detail carries the load error, so the caller's log says which file.
        assert "unavailable" in str(caught.value.detail)


class TestConcurrencyBoundsTheExpensiveWork:
    """_MAX_CONCURRENCY must bound what actually costs memory.

    The Dockerfile pins it to 1 and says why: "unbounded memory when several
    long recordings arrive together". The dominant term is not the ONNX graph —
    it is ffmpeg plus the decoded PCM, 32 kB per second of audio, ~115 MB for an
    hour. Guarding only the inference call left every concurrent upload decoding
    at once, so the setting bounded the cheap half and documented the expensive
    half.
    """

    @pytest.fixture(autouse=True)
    def _fresh_semaphore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The module-level semaphore binds to whichever loop first awaits it,
        # and each test gets its own loop. Production has one loop for the
        # process lifetime, so this is a test-isolation concern only.
        monkeypatch.setattr(svc, "_SEMAPHORE", asyncio.Semaphore(1))

    @staticmethod
    def _upload() -> Any:
        from starlette.datastructures import Headers, UploadFile

        return UploadFile(
            filename="a.wav",
            file=io.BytesIO(b"RIFF-ish bytes; the decoder is patched out"),
            headers=Headers({"content-type": "audio/wav"}),
        )

    async def test_two_requests_never_decode_at_the_same_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = 0
        peak = 0

        async def slow_decode(raw: bytes, suffix: str) -> bytes:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return b"\x00\x00" * svc.SILERO_CHUNK_SAMPLES

        monkeypatch.setattr(svc, "_decode_to_pcm16", slow_decode)
        monkeypatch.setattr(svc, "_require_session", lambda: object())
        monkeypatch.setattr(svc, "probabilities", lambda session, pcm, deadline=None: [0.0])

        await asyncio.gather(
            *(
                svc.segment(audio=self._upload(), min_segment_sec=0.5, max_segment_sec=30.0)
                for _ in range(4)
            ),
        )

        assert peak == 1, f"{peak} concurrent decodes under _MAX_CONCURRENCY=1"

    async def test_a_request_that_cannot_get_a_slot_is_told_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A waiter holds its raw upload resident, so the queue must be bounded.

        Answering 503 inside the caller's read timeout is also the difference
        between a log line naming the cause and a bare client-side timeout.
        """
        from fastapi import HTTPException

        from audio_graphy.adapters.real import vad_silero

        assert svc._QUEUE_WAIT_SEC < vad_silero._DEFAULT_TIMEOUT

        async def slow_decode(raw: bytes, suffix: str) -> bytes:
            await asyncio.sleep(0.2)
            return b"\x00\x00" * svc.SILERO_CHUNK_SAMPLES

        monkeypatch.setattr(svc, "_decode_to_pcm16", slow_decode)
        monkeypatch.setattr(svc, "_require_session", lambda: object())
        monkeypatch.setattr(svc, "probabilities", lambda session, pcm, deadline=None: [0.0])
        monkeypatch.setattr(svc, "_QUEUE_WAIT_SEC", 0.01)

        holder = asyncio.create_task(
            svc.segment(audio=self._upload(), min_segment_sec=0.5, max_segment_sec=30.0)
        )
        await asyncio.sleep(0.01)
        with pytest.raises(HTTPException) as caught:
            await svc.segment(audio=self._upload(), min_segment_sec=0.5, max_segment_sec=30.0)
        await holder

        assert caught.value.status_code == 503
        assert "slot" in str(caught.value.detail)


class TestResourceCeilings:
    """Every ceiling must fail loudly at the ceiling, not somewhere past it."""

    async def test_the_deadline_stops_the_inference_thread_itself(self) -> None:
        """asyncio.wait_for cannot cancel a thread; the loop must stop itself.

        Without the in-loop check, a timeout answered the caller 503 while the
        worker thread kept a core busy to the end of the recording — with
        _MAX_CONCURRENCY=1, the freed slot's next request then contended with
        a computation nobody was waiting for.
        """
        import time

        calls = 0

        class _SlowSession:
            def get_inputs(self):
                return [type("I", (), {"name": "input"})()]

            def run(self, _none, _feed):
                nonlocal calls
                calls += 1
                time.sleep(0.005)
                return [0.5]

        total_windows = 400
        pcm = b"\x00\x00" * (svc.SILERO_CHUNK_SAMPLES * total_windows)
        with pytest.raises(TimeoutError):
            svc.probabilities(_SlowSession(), pcm, deadline=time.monotonic() + 0.02)
        assert calls < total_windows // 4, (
            f"{calls} windows ran after the deadline — the loop is not checking it"
        )

    async def test_a_full_tmpfs_answers_507_and_leaks_no_temp_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file must die even when writing it is what failed.

        It used to be created outside the try with delete=False: an ENOSPC on
        the write leaked it permanently, shrinking the very tmpfs whose
        exhaustion caused the failure — each failed request made the next one
        more likely to fail.
        """
        import tempfile as _tempfile

        from fastapi import HTTPException

        created: list[str] = []
        real = _tempfile.NamedTemporaryFile

        def _failing(*args: object, **kwargs: object):
            handle = real(*args, **kwargs)  # type: ignore[arg-type]
            created.append(handle.name)

            class _Wrapper:
                name = handle.name

                def __enter__(self):
                    return self

                def __exit__(self, *exc: object) -> None:
                    handle.close()

                def write(self, _data: bytes) -> int:
                    raise OSError(28, "No space left on device")

            return _Wrapper()

        monkeypatch.setattr(svc.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        monkeypatch.setattr(svc.tempfile, "NamedTemporaryFile", _failing)

        with pytest.raises(HTTPException) as caught:
            await svc._decode_to_pcm16(b"bytes", ".wav")

        assert caught.value.status_code == 507
        assert created, "the failing write must have gone through the wrapper"
        assert not svc.Path(created[0]).exists(), "the temp file leaked"

    async def test_audio_past_the_duration_cap_is_refused_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated tail would read as "the rest had no speech".

        ffmpeg is capped at _MAX_AUDIO_SEC + 1s; output longer than the cap
        proves the input kept going, and the honest answer is 422 with the
        variable to raise — never a silently shortened segment list.
        """
        import asyncio as _asyncio

        from fastapi import HTTPException

        cap_sec = 1.0
        monkeypatch.setattr(svc, "_MAX_AUDIO_SEC", cap_sec)
        monkeypatch.setattr(svc.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

        class _FakeProcess:
            returncode = 0

            async def communicate(self):
                over = int(cap_sec * svc.SILERO_SAMPLE_RATE * 2) + 2
                return b"\x00" * over, b""

            def kill(self) -> None: ...

            async def wait(self) -> None: ...

        async def _fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
            assert "-t" in [str(a) for a in args], "the ffmpeg cap flag went missing"
            return _FakeProcess()

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fake_exec)

        with pytest.raises(HTTPException) as caught:
            await svc._decode_to_pcm16(b"bytes", ".wav")

        assert caught.value.status_code == 422
        assert "SILERO_VAD_MAX_AUDIO_SEC" in str(caught.value.detail)

    async def test_an_oversized_upload_is_refused_before_being_copied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UploadFile.size is the spooled truth; reject on it, not after read().

        Reading first would materialise a second copy of an upload we already
        know we are going to refuse.
        """
        import io

        from fastapi import HTTPException
        from starlette.datastructures import Headers, UploadFile

        monkeypatch.setattr(svc, "_require_session", lambda: object())

        class _MustNotRead(io.BytesIO):
            def read(self, *_a: object) -> bytes:  # type: ignore[override]
                raise AssertionError("the oversized upload was read anyway")

        upload = UploadFile(
            filename="big.wav",
            file=_MustNotRead(b""),
            size=svc._MAX_UPLOAD_BYTES + 1,
            headers=Headers({"content-type": "audio/wav"}),
        )
        with pytest.raises(HTTPException) as caught:
            await svc.segment(audio=upload, min_segment_sec=0.5, max_segment_sec=30.0)

        assert caught.value.status_code == 413


class TestTheContainerCanActuallyStart:
    """The Dockerfile's COPY list must equal the service's import closure.

    A wrong list is invisible everywhere except in the container: the build
    succeeds, every test passes (they run against the full source tree), and the
    image dies at `import audio_graphy.services.silero_vad_service`.

    That is exactly what shipped. The framing constants used to live in
    `adapters.real.streaming_vad_silero`, so importing them executed
    `adapters/__init__.py` -> `adapters.bundle` -> the LLM, embedding and ASR
    adapters. The list copied eight files and needed thirteen.
    """

    @staticmethod
    def _copied_modules() -> set[str]:
        import re
        from pathlib import Path

        dockerfile = (
            Path(__file__).resolve().parents[3] / "docker" / "silero-vad-service" / "Dockerfile"
        )
        return {
            match.group(1)
            for match in re.finditer(
                r"^COPY\s+--chown=\S+\s+(audio_graphy/\S+\.py)\s", dockerfile.read_text(), re.M
            )
        }

    def test_the_import_closure_is_exactly_what_the_dockerfile_copies(self) -> None:
        """Computed by importing in a subprocess whose sys.path holds ONLY the
        copied files — the editable install in this venv would otherwise satisfy
        every missing module from the source tree and hide the whole defect.
        """
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        backend = Path(__file__).resolve().parents[2]
        site = next(p for p in sys.path if p.endswith("site-packages"))
        copied = self._copied_modules()
        assert copied, "no COPY lines parsed — the Dockerfile shape changed"

        with tempfile.TemporaryDirectory() as staging, tempfile.TemporaryDirectory() as cwd:
            for rel in copied:
                dst = Path(staging) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(backend / rel, dst)
            probe = (
                "import sys;"
                "sys.path=[p for p in sys.path if 'WorkPlace/audio_graphy' not in p];"
                f"sys.path.insert(0,{staging!r});sys.path.append({site!r});"
                "import audio_graphy.services.silero_vad_service as m;"
                "print(m.SILERO_CHUNK_SAMPLES)"
            )
            result = subprocess.run(
                [sys.executable, "-S", "-c", probe],
                cwd=cwd,
                env={**os.environ, "PYTHONNOUSERSITE": "1"},
                capture_output=True,
                text=True,
            )

        assert result.returncode == 0, (
            "the image would die at import with only the copied files:\n"
            + result.stderr.strip().splitlines()[-1]
        )
        assert result.stdout.strip() == "512"
