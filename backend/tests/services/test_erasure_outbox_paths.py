"""`_erase_audio_paths` — the absent-file branch must be visible, not silent.

The tolerance itself is load-bearing (erasure retries after partial failure, and
a file removed by the previous attempt must be a no-op or the outbox row can
never reach ``succeeded``), so these tests pin BOTH properties: absence never
raises, and absence is logged with the worker and working_dir that observed it —
because on a multi-deployment database the same branch covers audio that
survives on another stack's volume while the DSAR is recorded as fulfilled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from audio_graphy.services.erasure_outbox import ErasureOutboxProcessor

pytestmark = pytest.mark.unit


def _processor(working_dir: Path) -> ErasureOutboxProcessor:
    # session_factory is untouched by _erase_audio_paths; a sentinel suffices.
    def _unused_factory() -> Any:  # pragma: no cover - never called
        raise AssertionError("_erase_audio_paths must not open a session")

    return ErasureOutboxProcessor(
        _unused_factory,  # type: ignore[arg-type]
        working_dir=working_dir,
        worker_id="test-worker-1",
    )


def test_absent_path_is_tolerated_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    processor = _processor(tmp_path)

    with caplog.at_level("WARNING", logger="audio_graphy.services.erasure_outbox"):
        processor._erase_audio_paths([str(tmp_path / "long-gone.wav")])

    warning = next(r for r in caplog.records if "already absent" in r.message)
    # The log must carry enough context to attribute the observation to a
    # deployment: which worker looked, and inside which working_dir.
    rendered = warning.getMessage()
    assert "test-worker-1" in rendered
    assert str(tmp_path) in rendered


def test_present_file_is_still_deleted_without_noise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "erase-me.wav"
    target.write_bytes(b"\x00" * 16)
    processor = _processor(tmp_path)

    with caplog.at_level("WARNING", logger="audio_graphy.services.erasure_outbox"):
        processor._erase_audio_paths([str(target)])

    assert not target.exists()
    assert not any("already absent" in r.message for r in caplog.records)
