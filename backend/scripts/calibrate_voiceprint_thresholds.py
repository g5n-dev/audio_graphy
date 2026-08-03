"""Recommend VOICEPRINT_COSINE_THRESHOLD / _AMBIGUOUS_THRESHOLD from data.

Both thresholds are ordinary settings, so changing them is easy. What was
missing is any basis for choosing them: the defaults 0.5 / 0.7 were never
calibrated against this deployment's audio, and the "CAM++ paper recommends
0.5" note in config.py is doubtful — ModelScope's own SV pipeline defaults
to roughly 0.3.

This script closes that loop. Give it a trial file, and it extracts each
clip through the configured voiceprint adapter, builds the same-speaker and
different-speaker cosine distributions, and reports where to put each
threshold.

    # CN-Celeb style: "<enrollment> <test> <0|1>" per line
    python scripts/calibrate_voiceprint_thresholds.py --trials trials.txt

It runs against whichever adapter is configured, so ADAPTER_VOICEPRINT_MODE=mock
exercises the tooling end to end without a model server — useful for
verifying the workflow, useless as a real recommendation. The output says
which mode produced it.

How the two thresholds are chosen:

  ambiguous_threshold — the lowest cosine at which a match is trustworthy
      enough to merge silently. Picked at the target false-accept rate
      (``--max-far``, default 1%): above it, at most that fraction of
      different-speaker pairs would be wrongly merged.
  cosine_threshold — the floor below which speakers are not merged at all.
      Picked at the equal-error point, which balances splitting one person
      into several nodes against merging two people into one.

Everything between the two is merged but tagged AMBIGUOUS and de-ranked in
retrieval, which is the band this design uses to buy back recall without
silently asserting identity.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make `audio_graphy` importable when run as a plain script (python scripts/...).
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audio_graphy.config import build_adapters, get_settings  # noqa: E402
from audio_graphy.eval.metrics.voiceprint import (  # noqa: E402
    parse_trial_file,
    voiceprint_eer_from_trials,
)

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--trials",
        required=True,
        type=Path,
        help="Trial file: '<enrollment_path> <test_path> <0|1>' per line.",
    )
    parser.add_argument(
        "--max-far",
        type=float,
        default=0.01,
        help="Target false-accept rate for the unambiguous threshold (default 0.01).",
    )
    parser.add_argument(
        "--mock-speaker-from",
        choices=("dirname", "filename"),
        default=None,
        help=(
            "Mock mode only: where to read speaker identity from, so the "
            "workflow can be exercised without a model server. 'dirname' "
            "suits one-directory-per-speaker corpora (CN-Celeb's data/ "
            "tree); 'filename' suits a flat directory of alice_01.wav. "
            "Never meaningful on timestamp or UUID names."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not args.trials.is_file():
        logger.error("Trial file not found: %s", args.trials)
        return 2
    if not 0.0 < args.max_far < 1.0:
        logger.error("--max-far must be between 0 and 1, got %s", args.max_far)
        return 2

    settings = get_settings()
    adapters = build_adapters(settings)
    if adapters.voiceprint is None:
        logger.error(
            "No voiceprint adapter configured; set ADAPTER_VOICEPRINT_MODE "
            "(and ENABLE_VOICEPRINT=true)."
        )
        return 2

    voiceprint = adapters.voiceprint
    if settings.adapter_voiceprint_mode != "real" and args.mock_speaker_from:
        # The mock cannot hear anything, so without this every clip is its own
        # speaker and the EER is pure noise. Opt in only when the trial clips
        # are named by speaker; on timestamp or UUID filenames the heuristic
        # silently merges unrelated people.
        from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter

        voiceprint = MockVoiceprintAdapter(
            speaker_from_filename=args.mock_speaker_from
        )

    trials = parse_trial_file(args.trials)
    if not trials:
        logger.error("Trial file parsed to zero usable trials: %s", args.trials)
        return 2

    logger.info(
        "Scoring %d trial pair(s) via the %s adapter…",
        len(trials),
        settings.adapter_voiceprint_mode,
    )
    try:
        result = await voiceprint_eer_from_trials(trials, voiceprint)
    finally:
        close = getattr(voiceprint, "aclose", None)
        if close is not None:
            await close()

    if result.skipped:
        logger.error(
            "Could not score the trials (no usable same/different pairs). "
            "Check that every path in the trial file is readable."
        )
        return 1

    unambiguous = result.threshold_at_far(args.max_far)
    eer_threshold = result.threshold

    # Settings reject cosine_threshold > ambiguous_threshold, so a
    # recommendation in that order is one a user cannot apply. It happens
    # whenever the data separates well: the FAR target is then met *below*
    # the equal-error point. Clamp the merge floor to the stricter value and
    # say so, rather than printing a pair that makes the service refuse to
    # start.
    inverted = (
        eer_threshold is not None
        and unambiguous is not None
        and eer_threshold > unambiguous
    )
    if inverted:
        assert eer_threshold is not None and unambiguous is not None
        eer_threshold = unambiguous
    # Never recommend a value the validator rejects outright.
    if eer_threshold is not None and not 0.0 <= eer_threshold <= 1.0:
        eer_threshold = None
    if eer_threshold is None:
        # Half a recommendation is worse than none: applying only the
        # AMBIGUOUS value leaves it paired with the existing merge floor,
        # which Settings may then reject as inverted.
        unambiguous = None

    lines = [
        "",
        f"adapter mode      : {settings.adapter_voiceprint_mode}",
    ]
    if settings.adapter_voiceprint_mode != "real":
        lines.append("  ! mock adapter — this verifies the workflow, not your audio.")
        if args.mock_speaker_from:
            lines.append(
                f"  ! speaker identity was taken from the {args.mock_speaker_from}, "
                "not from audio."
            )
    lines += [
        f"trials scored     : {len(trials)}",
        f"same/diff pairs   : {result.same_speaker_count} / {result.diff_speaker_count}",
        f"EER               : {result.eer:.4f}"
        + (f"  (threshold {eer_threshold:.4f})" if eer_threshold is not None else ""),
        f"  FAR at EER      : {result.far_at_eer:.4f}",
        f"  FRR at EER      : {result.frr_at_eer:.4f}",
        "",
        "Recommended settings:",
    ]
    lines.append(
        f"  VOICEPRINT_COSINE_THRESHOLD={eer_threshold:.2f}"
        if eer_threshold is not None
        else "  VOICEPRINT_COSINE_THRESHOLD=<undefined: no equal-error point>"
    )
    if unambiguous is not None:
        lines.append(f"  VOICEPRINT_AMBIGUOUS_THRESHOLD={unambiguous:.2f}")
        if inverted:
            lines += [
                "",
                "  The FAR target is met below the equal-error point, so the merge",
                "  floor was clamped up to match it: these trials separate well",
                "  enough that anything the equal-error point would have admitted is",
                "  already safe to merge outright. The unclamped equal-error point",
                f"  was {result.threshold:.4f}.",
            ]
        elif eer_threshold is not None and abs(unambiguous - eer_threshold) < 0.005:
            lines += [
                "",
                "  Both thresholds landed on the same value: these trials separate",
                "  cleanly, so there is no score band where a match is plausible but",
                "  unproven, and nothing would ever be tagged AMBIGUOUS. That is",
                "  expected for mock audio or an easy trial set, and unusual for real",
                "  recordings — if you see it on production audio, the trial set is",
                "  probably not representative (too few speakers, or clips that are",
                "  too clean and too long).",
            ]
    else:
        lines += [
            f"  VOICEPRINT_AMBIGUOUS_THRESHOLD=<none reaches FAR <= {args.max_far:.0%}>",
            "  No threshold separates the speakers well enough for a silent merge",
            "  at that precision. Either widen --max-far knowingly, or treat every",
            "  merge as reviewable.",
        ]
    lines += [
        "",
        f"current           : {settings.voiceprint_cosine_threshold} / "
        f"{settings.voiceprint_ambiguous_threshold}",
        "Nothing was changed — set these in .env yourself, and re-run the backfill",
        "only if you widen the merge band.",
    ]
    logger.info("%s", "\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
