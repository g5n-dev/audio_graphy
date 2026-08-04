"""Batch VAD service — the segmentation rules, and the contract it must satisfy.

Two classes of test here. The merging rules are pure and get exercised against
synthetic probability sequences. The rest pin the seams that would otherwise
fail only in production: the adapter's wire contract, and the constants shared
with the streaming path.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.adapters.real import streaming_vad_silero
from audio_graphy.services import silero_vad_service as svc

pytestmark = pytest.mark.unit

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

    def test_the_route_and_field_names_match_what_the_adapter_sends(self) -> None:
        import inspect

        from audio_graphy.adapters.real import vad_silero

        assert vad_silero._VAD_PATH == "/v1/vad/segment"
        source = inspect.getsource(vad_silero.SileroVADAdapter.segment)
        assert '"audio"' in source, "adapter's multipart part name"
        assert '"min_segment_sec"' in source
        assert '"max_segment_sec"' in source

        params = inspect.signature(svc.segment).parameters
        assert set(params) == {"audio", "min_segment_sec", "max_segment_sec"}

    def test_the_response_keys_are_the_ones_the_adapter_parses(self) -> None:
        import inspect

        from audio_graphy.adapters.real import vad_silero

        parser = inspect.getsource(vad_silero.SileroVADAdapter._parse_segments)
        for key in ("segments", "start_sec", "end_sec"):
            assert f'"{key}"' in parser or f"['{key}']" in parser or f'["{key}"]' in parser

    def test_the_adapter_timeout_bounds_the_service_timeout(self) -> None:
        """The service must give up before the client does.

        Otherwise the adapter times out at 30s while the service keeps the
        single concurrency slot for the rest of the run, and the retry queues
        behind work nobody is waiting for.
        """
        from audio_graphy.adapters.real import vad_silero

        assert svc._INFER_TIMEOUT_SEC < vad_silero._DEFAULT_TIMEOUT
