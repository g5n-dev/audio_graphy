"""Build a trial file for scripts/calibrate_voiceprint_thresholds.py.

The calibrator needs pairs of clips labelled "same speaker" or "different
speaker". This produces that file from either of the two layouts you are
likely to have:

``--from-dir`` — one directory per speaker::

    <root>/id00042/interview-01-001.flac
    <root>/id00042/singing-02-003.flac
    <root>/id00099/...

    This is CN-Celeb's ``data/`` layout, and also the obvious way to
    organise a hand-labelled set of your own recordings. Pairs are sampled
    from it deterministically.

``--from-cnceleb-trials`` — CN-Celeb's official evaluation list::

    python scripts/build_voiceprint_trials.py \\
        --from-cnceleb-trials /data/CN-Celeb/eval/lists/trials.lst \\
        --enroll-list /data/CN-Celeb/eval/enroll/lst \\
        --audio-root /data/CN-Celeb/eval \\
        --out trials.txt

    Use this when you want a number comparable to published results
    (CAM++ reports roughly 6.8% EER on CN-Celeb). Note one deliberate
    simplification: CN-Celeb enrolls a speaker by concatenating several
    utterances, while extract_voiceprint takes a single file, so the first
    enrollment utterance is used. That costs a little accuracy on the
    enrollment side and makes the resulting EER slightly pessimistic.

Which corpus to use is already decided in docs/DESIGN.md §8: CN-Celeb for
voiceprint EER, AliMeeting for diarization DER. Whatever you measure on a
public corpus is that corpus's acoustics, not yours — treat it as a
defensible starting point and re-run against your own labelled recordings
once you have a few hundred pairs.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import random
import sys
from pathlib import Path

# Make `audio_graphy` importable when run as a plain script (python scripts/...).
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus"})
# The trial format is whitespace-delimited, so a path containing whitespace
# cannot be represented. Such files are skipped rather than silently
# producing malformed lines the parser would drop later.
_UNREPRESENTABLE = (" ", "\t")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-dir",
        type=Path,
        help="Root holding one directory per speaker.",
    )
    source.add_argument(
        "--from-cnceleb-trials",
        type=Path,
        help="CN-Celeb official trials.lst to convert.",
    )
    parser.add_argument(
        "--enroll-list",
        type=Path,
        help="CN-Celeb eval/enroll/lst (required with --from-cnceleb-trials).",
    )
    parser.add_argument(
        "--audio-root",
        type=Path,
        help="Prefix for the relative paths in the CN-Celeb lists.",
    )
    parser.add_argument("--out", type=Path, required=True, help="Trial file to write.")
    parser.add_argument(
        "--positives-per-speaker",
        type=int,
        default=10,
        help="--from-dir: same-speaker pairs to draw per speaker (default 10).",
    )
    parser.add_argument(
        "--negatives-per-positive",
        type=int,
        default=5,
        help=(
            "--from-dir: different-speaker pairs per positive (default 5). "
            "A false-accept target of 1%% needs well over a hundred negatives "
            "before the estimate means anything."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260803,
        help="Sampling seed, so two runs are comparable (default fixed).",
    )
    return parser.parse_args(argv)


def _usable_clips(speaker_dir: Path) -> list[Path]:
    """Audio files under one speaker, sorted for deterministic sampling."""
    clips = [
        path
        for path in sorted(speaker_dir.rglob("*"))
        if path.suffix.lower() in _AUDIO_SUFFIXES and path.is_file()
    ]
    return [c for c in clips if not any(ch in str(c) for ch in _UNREPRESENTABLE)]


def _pairs_from_dir(
    root: Path,
    *,
    positives_per_speaker: int,
    negatives_per_positive: int,
    seed: int,
) -> tuple[list[tuple[str, str, int]], dict[str, int]]:
    """Sample same/different-speaker pairs from a speaker-per-directory tree."""
    by_speaker: dict[str, list[Path]] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        clips = _usable_clips(entry)
        # One clip cannot form a same-speaker pair, so such a speaker can
        # only ever contribute negatives — keep it for exactly that.
        if clips:
            by_speaker[entry.name] = clips

    stats = {
        "speakers": len(by_speaker),
        "clips": sum(len(v) for v in by_speaker.values()),
        "speakers_without_a_pair": sum(1 for v in by_speaker.values() if len(v) < 2),
    }
    if len(by_speaker) < 2:
        raise ValueError(
            f"need at least 2 speaker directories under {root}, found "
            f"{len(by_speaker)}"
        )

    # Seeded and reproducible on purpose — two runs must produce the same
    # trial set or the thresholds they yield cannot be compared. A
    # cryptographic generator would defeat that.
    rng = random.Random(seed)  # noqa: S311
    pairs: list[tuple[str, str, int]] = []
    speakers = list(by_speaker)

    for speaker, clips in by_speaker.items():
        if len(clips) < 2:
            continue
        combos = list(itertools.combinations(clips, 2))
        rng.shuffle(combos)
        chosen = combos[:positives_per_speaker]
        for left, right in chosen:
            pairs.append((str(left), str(right), 1))
            others = [s for s in speakers if s != speaker]
            for _ in range(negatives_per_positive):
                other = rng.choice(others)
                pairs.append((str(left), str(rng.choice(by_speaker[other])), 0))

    rng.shuffle(pairs)
    return pairs, stats


def _pairs_from_cnceleb(
    trials: Path,
    enroll_list: Path,
    audio_root: Path,
) -> tuple[list[tuple[str, str, int]], dict[str, int]]:
    """Convert CN-Celeb's official evaluation list.

    ``eval/enroll/lst`` maps an enrollment id to a comma-separated list of
    utterances. Only the first is used: extract_voiceprint takes one file,
    and concatenating them would mean writing new audio.
    """
    enroll: dict[str, str] = {}
    for raw in enroll_list.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        enroll[parts[0]] = parts[1].split(",")[0]

    pairs: list[tuple[str, str, int]] = []
    unknown_enroll = 0
    malformed = 0
    for raw in trials.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) != 3:
            if raw.strip():
                malformed += 1
            continue
        enroll_id, test_rel, label = parts
        utt = enroll.get(enroll_id)
        if utt is None:
            unknown_enroll += 1
            continue
        pairs.append(
            (
                str(audio_root / "enroll" / utt),
                str(audio_root / "test" / test_rel),
                1 if label.strip() in {"1", "target"} else 0,
            )
        )

    return pairs, {
        "enroll_ids": len(enroll),
        "unknown_enroll_ids": unknown_enroll,
        "malformed_lines": malformed,
    }


def _write(out: Path, pairs: list[tuple[str, str, int]]) -> dict[str, int]:
    """Write the trial file, dropping pairs whose audio is not readable.

    Dropping here rather than at scoring time is deliberate: the calibrator
    would silently skip unreadable clips, quietly shrinking the trial set
    behind the operator's back.
    """
    kept: list[str] = []
    missing = 0
    for enrollment, test, label in pairs:
        if not Path(enrollment).is_file() or not Path(test).is_file():
            missing += 1
            continue
        kept.append(f"{enrollment} {test} {label}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    positives = sum(1 for line in kept if line.rsplit(" ", 1)[1] == "1")
    return {
        "written": len(kept),
        "positives": positives,
        "negatives": len(kept) - positives,
        "dropped_missing_audio": missing,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    if args.from_cnceleb_trials is not None:
        if args.enroll_list is None or args.audio_root is None:
            logger.error(
                "--from-cnceleb-trials needs --enroll-list and --audio-root."
            )
            return 2
        for path in (args.from_cnceleb_trials, args.enroll_list):
            if not path.is_file():
                logger.error("Not a file: %s", path)
                return 2
        pairs, stats = _pairs_from_cnceleb(
            args.from_cnceleb_trials, args.enroll_list, args.audio_root
        )
    else:
        if not args.from_dir.is_dir():
            logger.error("Not a directory: %s", args.from_dir)
            return 2
        if args.positives_per_speaker < 1 or args.negatives_per_positive < 1:
            logger.error("Pair counts must be ≥ 1.")
            return 2
        try:
            pairs, stats = _pairs_from_dir(
                args.from_dir,
                positives_per_speaker=args.positives_per_speaker,
                negatives_per_positive=args.negatives_per_positive,
                seed=args.seed,
            )
        except ValueError as exc:
            logger.error("%s", exc)
            return 2

    if not pairs:
        logger.error("No trial pairs could be built; nothing written.")
        return 1

    written = _write(args.out, pairs)
    logger.info("source stats: %s", stats)
    logger.info("wrote %s: %s", args.out, written)
    if written["written"] == 0:
        logger.error(
            "Every pair referenced audio that does not exist — check the "
            "paths in the source lists."
        )
        return 1
    if written["negatives"] < 100:
        logger.warning(
            "Only %d different-speaker pairs. A 1%% false-accept target "
            "cannot be estimated from that few; the AMBIGUOUS threshold this "
            "produces will be noise.",
            written["negatives"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
