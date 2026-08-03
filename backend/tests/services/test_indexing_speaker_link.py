"""IndexingService speaker-linking stage — wiring + gating (ADR-0001).

The stage is what finally connects diarization to SpeakerLinker. These
tests pin the gating rules (every dependency must be present) and the
best-effort contract (a voiceprint outage must not fail indexing).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from audio_graphy.core.chunker import ChunkerOutput, SegmentRecord
from audio_graphy.services.indexing import IndexingService

pytestmark = pytest.mark.unit


@dataclass
class _Settings:
    """Only the fields the stage reads."""

    enable_voiceprint: bool = True
    voiceprint_sampling_strategy: str = "weighted_mean"
    voiceprint_sample_min_segment_sec: float = 1.0
    voiceprint_sample_min_total_sec: float = 3.0
    voiceprint_sample_max_segments: int = 8
    voiceprint_sample_outlier_cosine: float = 0.5
    voiceprint_cosine_threshold: float = 0.5
    voiceprint_ambiguous_threshold: float = 0.7
    enable_speaker_layer2_fuzzy: bool = False


@dataclass
class _Recording:
    id: int = 1
    tenant_id: str = "chang_an"
    path: str = "/tmp/rec.wav"
    recorded_at: Any = None


class _Bundle:
    def __init__(self, voiceprint: Any) -> None:
        self.voiceprint = voiceprint


class _FakeVoiceprint:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[float | None, float | None]] = []

    async def diarize(self, audio_path: str, **kwargs: object) -> Any:
        raise AssertionError("the linking stage must not re-run diarization")

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> Any:
        self.calls.append((start_sec, end_sec))
        if self.fail:
            raise RuntimeError("campplus down")

        @dataclass(frozen=True)
        class _R:
            vector: tuple[float, ...] = (1.0, 0.0, 0.0)
            dim: int = 3
            model: str = "test"
            speaker_id: str = ""
            duration_sec: float = 0.0

        return _R()


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> Any:
        return self._value if isinstance(self._value, list) else []


class _FakeSession:
    """Answers the two queries this stage issues, nothing more."""

    def __init__(self, *, already_linked: bool) -> None:
        self._already_linked = already_linked

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        rendered = str(statement)
        if "GET_LOCK" in rendered or "RELEASE_LOCK" in rendered:
            return _FakeResult(1)
        # The linked-labels query: report every label as done when the test
        # says this recording was already handled.
        return _FakeResult(
            ["spk_0", "spk_1"] if self._already_linked else []
        )


def _session_factory(*, already_linked: bool = False) -> Any:
    def _factory() -> _FakeSession:
        return _FakeSession(already_linked=already_linked)

    return _factory


def _service(
    *,
    settings: Any,
    crypto: Any,
    voiceprint: Any,
    already_linked: bool = False,
) -> IndexingService:
    """IndexingService with only the collaborators this stage touches."""
    return IndexingService(
        session_factory=_session_factory(already_linked=already_linked),
        bundle=_Bundle(voiceprint),  # type: ignore[arg-type]
        vector_store=None,  # type: ignore[arg-type]
        graph_store=None,  # type: ignore[arg-type]
        file_index=None,  # type: ignore[arg-type]
        settings=settings,
        audio_crypto=crypto,
    )


def _output(*segments: SegmentRecord, diarization: tuple[Any, ...] = ()) -> ChunkerOutput:
    return ChunkerOutput(
        recording_id=1,
        segments=list(segments),
        chunks=[],
        diarization=diarization,
    )


def _seg(start: float, end: float, speaker: str | None) -> SegmentRecord:
    return SegmentRecord(
        idx=0,
        start_sec=start,
        end_sec=end,
        transcript="",
        speaker=speaker,
        vad_conf=1.0,
    )


class TestVoiceprintGating:
    """Every dependency must be present, or the stage stays off."""

    def test_enabled_when_fully_wired(self) -> None:
        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=_FakeVoiceprint(),
        )
        assert svc._voiceprint_enabled is True

    def test_disabled_without_feature_flag(self) -> None:
        svc = _service(
            settings=_Settings(enable_voiceprint=False),
            crypto=object(),
            voiceprint=_FakeVoiceprint(),
        )
        assert svc._voiceprint_enabled is False

    def test_disabled_without_settings(self) -> None:
        """Callers that never wired settings keep the pre-ADR behaviour."""
        svc = _service(settings=None, crypto=object(), voiceprint=_FakeVoiceprint())
        assert svc._voiceprint_enabled is False

    def test_disabled_without_crypto(self) -> None:
        """Never write voiceprints when the at-rest key is missing (PIPL)."""
        svc = _service(
            settings=_Settings(),
            crypto=None,
            voiceprint=_FakeVoiceprint(),
        )
        assert svc._voiceprint_enabled is False

    def test_disabled_without_adapter(self) -> None:
        svc = _service(settings=_Settings(), crypto=object(), voiceprint=None)
        assert svc._voiceprint_enabled is False


@pytest.mark.asyncio
class TestSpeakerLinkStage:
    async def test_noop_when_disabled(self) -> None:
        adapter = _FakeVoiceprint()
        svc = _service(
            settings=_Settings(enable_voiceprint=False),
            crypto=object(),
            voiceprint=adapter,
        )
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )
        assert adapter.calls == []

    async def test_noop_without_diarized_segments(self) -> None:
        """No speaker labels means diarization never ran — nothing to sample."""
        adapter = _FakeVoiceprint()
        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=adapter,
        )
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, None)),
        )
        assert adapter.calls == []

    async def test_short_speaker_never_reaches_extraction(self) -> None:
        adapter = _FakeVoiceprint()
        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=adapter,
        )
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 1.5, "spk_0")),
        )
        assert adapter.calls == []

    async def test_extraction_failure_does_not_propagate(self) -> None:
        """A voiceprint outage must not fail an otherwise complete run."""
        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=_FakeVoiceprint(fail=True),
        )
        # Must not raise — the recording keeps its transcripts and graph.
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )

    async def test_linker_failure_does_not_propagate(self) -> None:
        """Sampling succeeds but the DB write fails — still best effort."""
        svc = _service(
            settings=_Settings(),
            crypto=object(),  # not a real AudioCrypto → SpeakerLinker will fail
            voiceprint=_FakeVoiceprint(),
        )
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )

    async def test_skips_when_every_speaker_is_already_linked(self) -> None:
        """Re-runs must not re-sample: the pipeline is retryable and linking
        is not idempotent (duplicate voiceprint_id aborts the candidate loop,
        and a successful merge would double-count the speaker's speech)."""
        adapter = _FakeVoiceprint()
        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=adapter,
            already_linked=True,
        )
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )
        # Not one extraction paid for on a retry.
        assert adapter.calls == []

    async def test_prefers_diarization_windows_over_vad_segments(self) -> None:
        """A VAD segment can span a speaker change; diarization cannot."""

        @dataclass(frozen=True)
        class _Diar:
            start_sec: float
            end_sec: float
            speaker_id: str

        adapter = _FakeVoiceprint()
        svc = _service(settings=_Settings(), crypto=object(), voiceprint=adapter)
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(
                # One long VAD segment straddling the hand-off, labelled spk_0.
                _seg(0.0, 40.0, "spk_0"),
                diarization=(
                    _Diar(0.0, 18.0, "spk_0"),
                    _Diar(18.0, 40.0, "spk_1"),
                ),
            ),
        )
        # Cropped on speaker boundaries, never on the 0-40 VAD window.
        assert (0.0, 40.0) not in adapter.calls
        assert sorted(adapter.calls) == [(0.0, 18.0), (18.0, 40.0)]

    async def test_falls_back_to_vad_labels_without_a_timeline(self) -> None:
        adapter = _FakeVoiceprint()
        svc = _service(settings=_Settings(), crypto=object(), voiceprint=adapter)
        await svc._stage_speaker_link(
            _Recording(),  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )
        assert adapter.calls == [(0.0, 30.0)]

    async def test_samples_the_recordings_own_audio(self) -> None:
        """Never a merged reception artifact (ADR-0001)."""
        captured: dict[str, Any] = {}

        class _PathCapturingVoiceprint(_FakeVoiceprint):
            async def extract_voiceprint(
                self,
                audio_path: str,
                *,
                speaker_id: str = "",
                start_sec: float | None = None,
                end_sec: float | None = None,
            ) -> Any:
                captured["audio_path"] = audio_path
                return await super().extract_voiceprint(
                    audio_path,
                    speaker_id=speaker_id,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )

        svc = _service(
            settings=_Settings(),
            crypto=object(),
            voiceprint=_PathCapturingVoiceprint(),
        )
        recording = replace(_Recording(), path="/data/recordings/original.wav")
        await svc._stage_speaker_link(
            recording,  # type: ignore[arg-type]
            _output(_seg(0.0, 30.0, "spk_0")),
        )
        assert captured["audio_path"] == "/data/recordings/original.wav"
