"""VoiceprintBackfill — historical recordings that predate ADR-0001.

The job's whole reason to exist is that those recordings were never
diarized, so it must re-diarize rather than trust stored segment labels.
These tests pin that, plus the guards that keep a batch job from
double-linking or dying on one bad file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from audio_graphy.core.voiceprint_backfill import VoiceprintBackfill

pytestmark = pytest.mark.unit


@dataclass
class _Settings:
    voiceprint_sampling_strategy: str = "weighted_mean"
    voiceprint_sample_min_segment_sec: float = 1.0
    voiceprint_sample_min_total_sec: float = 3.0
    voiceprint_sample_max_segments: int = 8
    voiceprint_sample_outlier_cosine: float = 0.5
    voiceprint_cosine_threshold: float = 0.5
    voiceprint_ambiguous_threshold: float = 0.7
    enable_speaker_layer2_fuzzy: bool = False


@dataclass(frozen=True)
class _Diar:
    start_sec: float
    end_sec: float
    speaker_id: str


@dataclass(frozen=True)
class _DiarResult:
    segments: tuple[_Diar, ...]


@dataclass(frozen=True)
class _Vec:
    vector: tuple[float, ...] = (1.0, 0.0, 0.0)
    dim: int = 3
    model: str = "test"
    speaker_id: str = ""
    duration_sec: float = 0.0


class _FakeVoiceprint:
    def __init__(
        self,
        *,
        segments: tuple[_Diar, ...] = (
            _Diar(0.0, 10.0, "spk_0"),
            _Diar(12.0, 24.0, "spk_1"),
        ),
        diarize_fails_for: set[str] | None = None,
    ) -> None:
        self._segments = segments
        self._diarize_fails_for = diarize_fails_for or set()
        self.diarized: list[str] = []
        self.extract_windows: list[tuple[float | None, float | None]] = []

    async def diarize(self, audio_path: str, **kwargs: object) -> _DiarResult:
        self.diarized.append(audio_path)
        if audio_path in self._diarize_fails_for:
            raise RuntimeError("campplus down")
        return _DiarResult(segments=self._segments)

    async def extract_voiceprint(
        self,
        audio_path: str,
        *,
        speaker_id: str = "",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> _Vec:
        self.extract_windows.append((start_sec, end_sec))
        # Distinct vector per speaker so linking does not collapse them.
        if speaker_id.endswith("1"):
            return _Vec(vector=(0.0, 1.0, 0.0))
        return _Vec(vector=(1.0, 0.0, 0.0))


@dataclass
class _RecordedLink:
    recording_id: int
    candidates: tuple[Any, ...]


class _FakeLinker:
    """Stands in for SpeakerLinker so these tests stay off the database."""

    def __init__(self) -> None:
        self.calls: list[_RecordedLink] = []

    async def run(self, recording_id: int, candidates: Any) -> Any:
        self.calls.append(_RecordedLink(recording_id, tuple(candidates)))

        @dataclass(frozen=True)
        class _R:
            new_speakers: int = len(tuple(candidates))
            merged_speakers: int = 0

        return _R()


@dataclass
class _FakeState:
    pending: list[tuple[int, str, Any]] = field(default_factory=list)
    linked_ids: set[int] = field(default_factory=set)


@pytest.fixture
def make_job(monkeypatch: pytest.MonkeyPatch):
    """Build a backfill job with the DB-bound collaborators stubbed out.

    Patches through ``monkeypatch`` so the module globals are restored —
    leaving them replaced would silently disable the advisory lock for
    every later test in the same process.
    """

    def _factory(state: _FakeState, adapter: Any, linker: Any) -> VoiceprintBackfill:
        import contextlib

        import audio_graphy.core.recording_speaker_link as link_module
        import audio_graphy.core.voiceprint_backfill as module

        @contextlib.asynccontextmanager
        async def _no_lock(*args: object, **kwargs: object) -> Any:
            yield True

        # The lock and the real linker live in the shared single-recording
        # module now; the backfill only builds the linker.
        monkeypatch.setattr(link_module, "tenant_advisory_lock", _no_lock)
        monkeypatch.setattr(module, "build_linker", lambda *a, **k: linker)

        job = VoiceprintBackfill(
            session_factory=None,  # type: ignore[arg-type]
            voiceprint=adapter,
            crypto=object(),  # type: ignore[arg-type]
            settings=_Settings(),
            tenant_id="t1",
        )

        async def _pending(*, limit: int, after_id: int = 0) -> list[tuple[int, str, Any]]:
            remaining = [row for row in state.pending if row[0] > after_id]
            return remaining[:limit]

        async def _linked_labels(*args: Any, **kwargs: Any) -> set[str]:
            recording_id = kwargs.get("recording_id", args[-1] if args else None)
            # Fully linked when the test marked this recording as taken.
            return {"spk_0", "spk_1"} if recording_id in state.linked_ids else set()

        monkeypatch.setattr(job, "pending_recordings", _pending)
        monkeypatch.setattr(link_module, "linked_speaker_labels", _linked_labels)
        return job

    return _factory


def _audio(tmp_path: Any, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"fake audio")
    return str(path)


@pytest.mark.asyncio
class TestBackfill:
    async def test_rediarizes_rather_than_trusting_stored_labels(
        self, tmp_path: Any, make_job: Any
    ) -> None:
        """The whole point: these recordings have no speaker labels stored."""
        audio = _audio(tmp_path, "a.wav")
        state = _FakeState(pending=[(1, audio, None)])
        adapter = _FakeVoiceprint()
        linker = _FakeLinker()
        job = make_job(state, adapter, linker)

        report = await job.run(limit=10)

        assert adapter.diarized == [audio]
        assert report.linked == 1
        # Crops follow the diarization windows, not any stored segment.
        assert sorted(adapter.extract_windows) == [(0.0, 10.0), (12.0, 24.0)]

    async def test_missing_audio_is_skipped_with_a_reason(
        self, tmp_path: Any, make_job: Any
    ) -> None:
        state = _FakeState(pending=[(4, str(tmp_path / "gone.wav"), None)])
        adapter = _FakeVoiceprint()
        job = make_job(state, adapter, _FakeLinker())

        report = await job.run(limit=10)

        assert report.linked == 0
        assert report.skipped[4] == "audio file missing"
        assert adapter.diarized == []

    async def test_one_failure_does_not_end_the_batch(self, tmp_path: Any, make_job: Any) -> None:
        good = _audio(tmp_path, "good.wav")
        bad = _audio(tmp_path, "bad.wav")
        state = _FakeState(pending=[(1, bad, None), (2, good, None)])
        adapter = _FakeVoiceprint(diarize_fails_for={bad})
        linker = _FakeLinker()
        job = make_job(state, adapter, linker)

        report = await job.run(limit=10)

        assert report.scanned == 2
        assert report.linked == 1
        assert "failed" in report.skipped[1]
        assert [c.recording_id for c in linker.calls] == [2]

    async def test_skips_a_recording_linked_while_we_diarized(
        self, tmp_path: Any, make_job: Any
    ) -> None:
        """The live pipeline may win the race; re-check under the lock."""
        audio = _audio(tmp_path, "a.wav")
        state = _FakeState(pending=[(9, audio, None)], linked_ids={9})
        linker = _FakeLinker()
        job = make_job(state, _FakeVoiceprint(), linker)

        report = await job.run(limit=10)

        assert report.linked == 0
        assert report.skipped[9] == "all speakers already linked"
        assert linker.calls == []

    async def test_speaker_below_the_gate_yields_no_candidate(
        self, tmp_path: Any, make_job: Any
    ) -> None:
        audio = _audio(tmp_path, "a.wav")
        state = _FakeState(pending=[(1, audio, None)])
        adapter = _FakeVoiceprint(segments=(_Diar(0.0, 1.5, "spk_0"),))
        linker = _FakeLinker()
        job = make_job(state, adapter, linker)

        report = await job.run(limit=10)

        assert report.linked == 0
        assert "quality gates" in report.skipped[1]
        assert linker.calls == []

    async def test_empty_diarization_is_reported(self, tmp_path: Any, make_job: Any) -> None:
        audio = _audio(tmp_path, "a.wav")
        state = _FakeState(pending=[(1, audio, None)])
        adapter = _FakeVoiceprint(segments=())
        job = make_job(state, adapter, _FakeLinker())

        report = await job.run(limit=10)

        assert report.skipped[1] == "diarization produced no speaker windows"

    async def test_limit_bounds_the_pass(self, tmp_path: Any, make_job: Any) -> None:
        state = _FakeState(pending=[(i, _audio(tmp_path, f"{i}.wav"), None) for i in range(1, 6)])
        adapter = _FakeVoiceprint()
        job = make_job(state, adapter, _FakeLinker())

        report = await job.run(limit=2)

        assert report.scanned == 2
        assert len(adapter.diarized) == 2

    async def test_rejects_a_non_positive_limit(self, tmp_path: Any, make_job: Any) -> None:
        job = make_job(_FakeState(), _FakeVoiceprint(), _FakeLinker())
        with pytest.raises(ValueError, match="limit must be"):
            await job.run(limit=0)

    async def test_unlinkable_recordings_do_not_block_later_ones(
        self, tmp_path: Any, make_job: Any
    ) -> None:
        """The cursor is what makes repeated runs progress.

        Recordings whose audio aged out never gain a link, so a "still
        unlinked" filter alone would refill every batch with the same oldest
        rows and never reach anything newer.
        """
        good = _audio(tmp_path, "good.wav")
        state = _FakeState(
            pending=[
                (1, str(tmp_path / "gone-1.wav"), None),
                (2, str(tmp_path / "gone-2.wav"), None),
                (3, good, None),
            ]
        )
        linker = _FakeLinker()
        job = make_job(state, _FakeVoiceprint(), linker)

        first = await job.run(limit=2)
        assert first.linked == 0
        assert first.last_scanned_id == 2

        second = await job.run(limit=2, after_id=first.last_scanned_id)
        assert second.linked == 1
        assert [c.recording_id for c in linker.calls] == [3]

    async def test_resumes_only_the_speakers_that_were_not_linked(
        self, tmp_path: Any, make_job: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that died between candidates must be finishable.

        Each candidate commits separately, so a recording can end up with
        spk_0 linked and spk_1 not. A recording-level guard would call that
        done forever and lose spk_1.
        """
        import audio_graphy.core.recording_speaker_link as link_module

        audio = _audio(tmp_path, "a.wav")
        state = _FakeState(pending=[(1, audio, None)])
        linker = _FakeLinker()
        job = make_job(state, _FakeVoiceprint(), linker)

        async def _half_done(*args: Any, **kwargs: Any) -> set[str]:
            return {"spk_0"}

        monkeypatch.setattr(link_module, "linked_speaker_labels", _half_done)

        report = await job.run(limit=10)

        assert report.linked == 1
        assert [c.speaker_id for c in linker.calls[0].candidates] == ["spk_1"]
