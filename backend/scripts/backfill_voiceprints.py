"""Backfill speaker voiceprints for recordings ingested before ADR-0001.

Enabling ``ENABLE_VOICEPRINT`` only affects recordings indexed afterwards.
Everything already in the database was chunked with diarization off, so it
carries no speaker labels and will never gain a cross-recording identity.
This job re-runs diarization on those recordings, then samples and links
them exactly as the live pipeline does.

Each recording costs a full-file diarization plus several extractions, so
the pass is bounded by ``--limit`` and is safe to run repeatedly: already
linked recordings are skipped, and progress is monotonic (oldest first).

    python scripts/backfill_voiceprints.py --tenant chang_an --limit 50
    python scripts/backfill_voiceprints.py --tenant chang_an --dry-run

Requires ENABLE_VOICEPRINT=true, a reachable campplus-service, and the
master key — the same prerequisites as the live pipeline.
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
from audio_graphy.core.crypto import AudioCrypto  # noqa: E402
from audio_graphy.core.voiceprint_backfill import VoiceprintBackfill  # noqa: E402
from audio_graphy.db import create_db_engine, create_session_factory  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant whose recordings should be backfilled.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum recordings to process in this pass (default: 20).",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=20,
        help="Stop after this many --limit-sized batches (default: 20).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many recordings are unlinked without processing them.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.enable_voiceprint:
        logger.error(
            "ENABLE_VOICEPRINT is false. Backfilling while the feature is off "
            "would populate speaker data the running system ignores; enable it first."
        )
        return 2

    adapters = build_adapters(settings)
    if adapters.voiceprint is None:
        logger.error("No voiceprint adapter is configured; check ADAPTER_VOICEPRINT_MODE.")
        return 2

    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        crypto = AudioCrypto(
            Path(str(settings.master_key_path)),
            chunk_size_bytes=settings.audio_crypto_chunk_size_bytes,
            max_plaintext_bytes=settings.max_recording_audio_bytes,
        )
        crypto.validate_master_key()

        job = VoiceprintBackfill(
            session_factory,
            adapters.voiceprint,
            crypto,
            settings,
            tenant_id=args.tenant,
        )

        if args.dry_run:
            pending = await job.pending_recordings(limit=args.limit)
            logger.info(
                "Dry run: %d unlinked recording(s) in tenant %s (showing up to --limit): %s",
                len(pending),
                args.tenant,
                [rid for rid, _, _ in pending],
            )
            return 0

        # Batches advance by recording id, not by "still unlinked": plenty of
        # recordings can never link (audio aged out, no speech, nobody clears
        # the gates) and would otherwise refill every batch forever.
        cursor = 0
        totals = {"scanned": 0, "linked": 0, "new": 0, "merged": 0}
        batches = 0
        while batches < args.max_batches:
            report = await job.run(limit=args.limit, after_id=cursor)
            if report.scanned == 0:
                break
            batches += 1
            totals["scanned"] += report.scanned
            totals["linked"] += report.linked
            totals["new"] += report.new_speakers
            totals["merged"] += report.merged_speakers
            for recording_id, reason in report.skipped.items():
                logger.info("  skipped recording %d: %s", recording_id, reason)
            cursor = report.last_scanned_id

        logger.info(
            "Backfill complete — scanned=%d linked=%d new_speakers=%d merged=%d "
            "(%d batch(es), cursor at recording %d)",
            totals["scanned"],
            totals["linked"],
            totals["new"],
            totals["merged"],
            batches,
            cursor,
        )
        if batches >= args.max_batches:
            logger.info(
                "Stopped at --max-batches; run again to continue past recording %d.",
                cursor,
            )
        return 0
    finally:
        close = getattr(adapters.voiceprint, "aclose", None)
        if close is not None:
            await close()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
