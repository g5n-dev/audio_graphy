"""build_voiceprint_trials — turning a corpus into a calibration trial file.

The trial file is the only input to threshold calibration, so a silent
mistake here (a speaker collapsed into one identity, pairs pointing at
files that do not exist) produces a confident-looking threshold built on
nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from build_voiceprint_trials import main  # noqa: E402

pytestmark = pytest.mark.unit


def _corpus(root: Path, speakers: int = 4, clips: int = 3) -> Path:
    for spk in range(speakers):
        directory = root / f"id{spk:05d}"
        directory.mkdir(parents=True)
        for clip in range(clips):
            (directory / f"interview-{clip:02d}.flac").write_bytes(b"x" * 32)
    return root


def _read(path: Path) -> list[tuple[str, str, str]]:
    return [
        tuple(line.split())  # type: ignore[misc]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestFromDirectory:
    def test_builds_labelled_pairs(self, tmp_path: Path) -> None:
        out = tmp_path / "trials.txt"
        code = main(
            [
                "--from-dir",
                str(_corpus(tmp_path / "corpus")),
                "--out",
                str(out),
                "--positives-per-speaker",
                "2",
                "--negatives-per-positive",
                "3",
            ]
        )
        assert code == 0
        rows = _read(out)
        positives = [r for r in rows if r[2] == "1"]
        negatives = [r for r in rows if r[2] == "0"]
        assert len(positives) == 8  # 4 speakers x 2
        assert len(negatives) == 24  # 8 positives x 3

    def test_positive_pairs_share_a_speaker_and_negatives_do_not(self, tmp_path: Path) -> None:
        """The labels are the ground truth the EER is computed against."""
        out = tmp_path / "trials.txt"
        main(["--from-dir", str(_corpus(tmp_path / "corpus")), "--out", str(out)])
        for enrollment, test, label in _read(out):
            same_speaker = Path(enrollment).parent == Path(test).parent
            assert same_speaker is (label == "1")

    def test_is_deterministic(self, tmp_path: Path) -> None:
        """Two runs must be comparable, so the sampling is seeded."""
        corpus = _corpus(tmp_path / "corpus")
        first, second = tmp_path / "a.txt", tmp_path / "b.txt"
        main(["--from-dir", str(corpus), "--out", str(first)])
        main(["--from-dir", str(corpus), "--out", str(second)])
        assert first.read_text() == second.read_text()

    def test_a_different_seed_gives_a_different_set(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        first, second = tmp_path / "a.txt", tmp_path / "b.txt"
        main(["--from-dir", str(corpus), "--out", str(first), "--seed", "1"])
        main(["--from-dir", str(corpus), "--out", str(second), "--seed", "2"])
        assert first.read_text() != second.read_text()

    def test_single_clip_speaker_contributes_only_negatives(self, tmp_path: Path) -> None:
        """One clip cannot form a same-speaker pair, and must not be faked."""
        corpus = _corpus(tmp_path / "corpus", speakers=2)
        lone = corpus / "id09999"
        lone.mkdir()
        (lone / "solo.flac").write_bytes(b"x" * 32)

        out = tmp_path / "trials.txt"
        main(["--from-dir", str(corpus), "--out", str(out)])
        rows = _read(out)
        assert not any(r[2] == "1" and "id09999" in r[0] and "id09999" in r[1] for r in rows)

    def test_rejects_a_corpus_with_one_speaker(self, tmp_path: Path) -> None:
        """A negative pair needs two speakers; better to refuse than emit junk."""
        code = main(
            [
                "--from-dir",
                str(_corpus(tmp_path / "corpus", speakers=1)),
                "--out",
                str(tmp_path / "trials.txt"),
            ]
        )
        assert code == 2

    def test_skips_paths_the_format_cannot_represent(self, tmp_path: Path) -> None:
        """The trial format is whitespace-delimited."""
        corpus = _corpus(tmp_path / "corpus", speakers=2, clips=2)
        bad = corpus / "id00000" / "has space.flac"
        bad.write_bytes(b"x" * 32)

        out = tmp_path / "trials.txt"
        main(["--from-dir", str(corpus), "--out", str(out)])
        assert "has space" not in out.read_text(encoding="utf-8")
        # Every line still parses into exactly three fields.
        assert all(len(row) == 3 for row in _read(out))


class TestFromCnCelebTrials:
    def test_resolves_enrollment_ids_and_labels(self, tmp_path: Path) -> None:
        root = tmp_path / "eval"
        (root / "enroll" / "id00800").mkdir(parents=True)
        (root / "test" / "id00800").mkdir(parents=True)
        (root / "enroll" / "id00800" / "a.flac").write_bytes(b"x")
        (root / "test" / "id00800" / "b.flac").write_bytes(b"x")

        # CN-Celeb enrolls from several utterances; only the first is usable
        # here because extract_voiceprint takes one file.
        enroll = tmp_path / "enroll.lst"
        enroll.write_text("id00800-enroll id00800/a.flac,id00800/second.flac\n", encoding="utf-8")
        trials = tmp_path / "trials.lst"
        trials.write_text("id00800-enroll id00800/b.flac 1\n", encoding="utf-8")

        out = tmp_path / "out.txt"
        code = main(
            [
                "--from-cnceleb-trials",
                str(trials),
                "--enroll-list",
                str(enroll),
                "--audio-root",
                str(root),
                "--out",
                str(out),
            ]
        )
        assert code == 0
        rows = _read(out)
        assert len(rows) == 1
        assert rows[0][0].endswith("enroll/id00800/a.flac")
        assert rows[0][1].endswith("test/id00800/b.flac")
        assert rows[0][2] == "1"

    def test_requires_its_companion_arguments(self, tmp_path: Path) -> None:
        trials = tmp_path / "trials.lst"
        trials.write_text("x y 1\n", encoding="utf-8")
        code = main(["--from-cnceleb-trials", str(trials), "--out", str(tmp_path / "o.txt")])
        assert code == 2


class TestMissingAudio:
    def test_pairs_pointing_at_absent_files_are_dropped(self, tmp_path: Path) -> None:
        """Keeping them would let the calibrator silently shrink the set."""
        root = tmp_path / "eval"
        (root / "enroll").mkdir(parents=True)
        (root / "test").mkdir(parents=True)
        enroll = tmp_path / "enroll.lst"
        enroll.write_text("e1 gone.flac\n", encoding="utf-8")
        trials = tmp_path / "trials.lst"
        trials.write_text("e1 alsogone.flac 1\n", encoding="utf-8")

        code = main(
            [
                "--from-cnceleb-trials",
                str(trials),
                "--enroll-list",
                str(enroll),
                "--audio-root",
                str(root),
                "--out",
                str(tmp_path / "o.txt"),
            ]
        )
        # Nothing survivable — a non-zero exit, not a silently empty file.
        assert code == 1
