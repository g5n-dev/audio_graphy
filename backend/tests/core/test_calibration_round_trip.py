"""Calibration → config → linking, as one loop.

Each piece has its own tests, but the loop is what the operator actually
performs: build a trial file, calibrate, put the two numbers in .env,
restart, and expect speakers to merge sensibly. A break anywhere along that
path produces a plausible-looking threshold that quietly links the wrong
people, and no single-component test would notice.

Runs on the mock adapter, so what it proves is that the *pipeline* is
coherent — not that any particular threshold is right for real audio.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter
from audio_graphy.config import Settings
from audio_graphy.eval.metrics.voiceprint import (
    parse_trial_file,
    voiceprint_eer_from_trials,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from build_voiceprint_trials import main as build_trials  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_SPEAKERS = 6
_CLIPS = 4


def _corpus(root: Path) -> Path:
    """A CN-Celeb-shaped corpus: one directory per speaker."""
    for speaker in range(_SPEAKERS):
        directory = root / f"id{speaker:05d}"
        directory.mkdir(parents=True)
        for clip in range(_CLIPS):
            (directory / f"interview-{clip:02d}.flac").write_bytes(b"audio" * 16)
    return root


async def _calibrate(trials_path: Path) -> tuple[float | None, float | None, float]:
    """The calibration script's core, minus its printing."""
    adapter = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="dirname")
    result = await voiceprint_eer_from_trials(parse_trial_file(trials_path), adapter)
    eer_threshold = result.threshold
    unambiguous = result.threshold_at_far(0.01)
    # The script's clamp: Settings reject cosine > ambiguous, and good
    # separation naturally produces that ordering.
    if (
        eer_threshold is not None
        and unambiguous is not None
        and eer_threshold > unambiguous
    ):
        eer_threshold = unambiguous
    return eer_threshold, unambiguous, result.eer


class TestCalibrationRoundTrip:
    async def test_recommended_thresholds_are_accepted_by_settings(
        self,
        tmp_path: Path,
    ) -> None:
        """The output has to be something an operator can actually apply.

        Settings enforce 0 ≤ cosine ≤ ambiguous ≤ 1; a recommendation that
        violates it makes the service refuse to start, which is a worse
        outcome than no recommendation at all.
        """
        trials = tmp_path / "trials.txt"
        assert (
            build_trials(
                ["--from-dir", str(_corpus(tmp_path / "corpus")), "--out", str(trials)]
            )
            == 0
        )

        cosine, ambiguous, eer = await _calibrate(trials)
        assert cosine is not None and ambiguous is not None
        assert eer == pytest.approx(0.0, abs=0.05), "mock separates cleanly"

        settings = Settings(
            working_dir=str(tmp_path / "wd"),
            master_key_path=str(tmp_path / "wd" / "k"),
            voiceprint_cosine_threshold=round(cosine, 2),
            voiceprint_ambiguous_threshold=round(ambiguous, 2),
        )
        assert settings.voiceprint_cosine_threshold <= (
            settings.voiceprint_ambiguous_threshold
        )

    async def test_calibrated_thresholds_separate_the_speakers_they_came_from(
        self,
        tmp_path: Path,
    ) -> None:
        """The loop closes only if the numbers work on their own corpus.

        A threshold that scores well during calibration but then merges two
        different speakers — or splits one — would mean the calibrator and
        the linker disagree about what a cosine means.
        """
        corpus = _corpus(tmp_path / "corpus")
        trials = tmp_path / "trials.txt"
        build_trials(["--from-dir", str(corpus), "--out", str(trials)])
        cosine, ambiguous, _ = await _calibrate(trials)
        assert cosine is not None and ambiguous is not None

        adapter = MockVoiceprintAdapter(latency_ms=0, speaker_from_filename="dirname")

        async def vec(path: Path) -> tuple[float, ...]:
            return (await adapter.extract_voiceprint(str(path))).vector

        def cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
            return sum(x * y for x, y in zip(a, b, strict=True))

        same_pairs: list[float] = []
        diff_pairs: list[float] = []
        speakers = sorted(p for p in corpus.iterdir() if p.is_dir())
        for speaker in speakers:
            clips = sorted(speaker.glob("*.flac"))
            same_pairs.append(cos(await vec(clips[0]), await vec(clips[1])))
        for left, right in itertools.pairwise(speakers):
            diff_pairs.append(
                cos(
                    await vec(sorted(left.glob("*.flac"))[0]),
                    await vec(sorted(right.glob("*.flac"))[0]),
                )
            )

        # Every same-speaker pair merges, and unambiguously so.
        assert all(score >= ambiguous for score in same_pairs), (
            f"same-speaker scores {same_pairs} vs ambiguous {ambiguous}"
        )
        # No different-speaker pair reaches even the merge floor.
        assert all(score < cosine for score in diff_pairs), (
            f"diff-speaker scores {diff_pairs} vs cosine {cosine}"
        )

    async def test_a_corpus_that_does_not_separate_yields_no_recommendation(
        self,
        tmp_path: Path,
    ) -> None:
        """Refusing to answer beats inventing a threshold.

        With identity unavailable — the mock's default, and the honest
        stand-in for audio the model cannot tell apart — there is no
        equal-error point, and the loop must stop rather than hand back a
        number that would be applied verbatim.
        """
        corpus = _corpus(tmp_path / "corpus")
        trials = tmp_path / "trials.txt"
        build_trials(["--from-dir", str(corpus), "--out", str(trials)])

        blind = MockVoiceprintAdapter(latency_ms=0)  # no identity fallback
        result = await voiceprint_eer_from_trials(parse_trial_file(trials), blind)

        assert result.eer > 0.3, "unrecognisable speakers must not score well"
        assert result.threshold is None or result.threshold_at_far(0.01) is None

    async def test_trial_file_survives_the_round_trip_verbatim(
        self,
        tmp_path: Path,
    ) -> None:
        """The generator writes what the parser reads — no silent losses."""
        corpus = _corpus(tmp_path / "corpus")
        trials = tmp_path / "trials.txt"
        build_trials(
            [
                "--from-dir",
                str(corpus),
                "--out",
                str(trials),
                "--positives-per-speaker",
                "3",
                "--negatives-per-positive",
                "4",
            ]
        )
        written = [
            line for line in trials.read_text(encoding="utf-8").splitlines() if line
        ]
        parsed = parse_trial_file(trials)
        assert len(parsed) == len(written)
        assert sum(1 for t in parsed if t.same_speaker) == _SPEAKERS * 3


class TestCalibratorAndLinkerAgree:
    async def test_the_sampler_produces_vectors_the_calibrator_scored(
        self,
        tmp_path: Path,
    ) -> None:
        """Both paths must normalize identically, or the numbers do not transfer.

        The calibrator scores raw ``extract_voiceprint`` output; the linker
        compares centroids the sampler built. If those differed in scale, a
        threshold measured on one would be meaningless to the other.

        The raw call passes the same ``speaker_id`` the sampler uses. A real
        adapter derives identity from the audio and would return the same
        vector either way; the mock derives it from the argument, so leaving
        it out here would compare two different mock identities and prove
        nothing about normalization.
        """
        from audio_graphy.core.voiceprint_sampler import VoiceprintSampler

        clip = tmp_path / "id00001" / "interview-00.flac"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"audio" * 16)

        adapter = MockVoiceprintAdapter(latency_ms=0)
        raw = (
            await adapter.extract_voiceprint(str(clip), speaker_id="spk_0")
        ).vector

        class _Seg:
            def __init__(self, start: float, end: float) -> None:
                self.start_sec = start
                self.end_sec = end
                self.speaker = "spk_0"

        report = await VoiceprintSampler(adapter, outlier_cosine=0.0).sample(
            recording_id=1,
            audio_path=str(clip),
            segments=[_Seg(0.0, 10.0)],  # type: ignore[list-item]
        )
        sampled = report.candidates[0].voiceprint

        # A single segment means the centroid is that segment, so the two
        # must agree up to normalization.
        cos = sum(x * y for x, y in zip(raw, sampled, strict=True))
        raw_norm = sum(x * x for x in raw) ** 0.5
        assert cos / raw_norm == pytest.approx(1.0, abs=1e-6)
        assert sum(x * x for x in sampled) ** 0.5 == pytest.approx(1.0, abs=1e-6)
