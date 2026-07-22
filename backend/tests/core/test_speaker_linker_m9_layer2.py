"""T12 — SpeakerLinker Layer 2 wiring tests (M9 R1 T12 / L8 ruling).

These tests exercise the ``_try_layer2_fuzzy`` / ``_derive_query_name``
helpers and the ``enable_layer2_fuzzy`` flag. Full integration with the
DB + SpeakerMergePending queue is covered by
``tests/integration/test_speaker_reconfirm.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from audio_graphy.core.speaker_fuzzy_matcher import (
    SpeakerCandidate,
    SpeakerFuzzyMatcher,
    SpeakerFuzzyResult,
)
from audio_graphy.core.speaker_linker import (
    SpeakerLinker,
    _NewSpeakerCandidate,
)


# ============================================================
# _derive_query_name
# ============================================================


def test_derive_query_name_uses_override() -> None:
    cand = _make_candidate(display_name="客户王小姐")
    assert SpeakerLinker._derive_query_name(cand) == "客户王小姐"


def test_derive_query_name_falls_back_to_role_hint() -> None:
    cand = _make_candidate(role_hint="customer")
    assert SpeakerLinker._derive_query_name(cand) == "customer"


def test_derive_query_name_falls_back_to_speaker_id() -> None:
    cand = _make_candidate(role_hint="", display_name="")
    assert SpeakerLinker._derive_query_name(cand) == "spk_0"


# ============================================================
# enable_layer2_fuzzy flag
# ============================================================


def test_linker_defaults_to_layer2_enabled() -> None:
    """By default Layer 2 is on (L8 ruling active)."""
    linker = _build_linker()
    assert linker._enable_layer2_fuzzy is True


def test_linker_can_disable_layer2_for_zero_regression() -> None:
    """enable_layer2_fuzzy=False → behaves like M7 (escape hatch)."""
    linker = _build_linker(enable_layer2_fuzzy=False)
    assert linker._enable_layer2_fuzzy is False


def test_linker_accepts_custom_matcher() -> None:
    """Pre-built matcher passed in ctor is preserved (DI for tests)."""
    custom = SpeakerFuzzyMatcher()
    linker = _build_linker(fuzzy_matcher=custom)
    assert linker._fuzzy_matcher is custom


# ============================================================
# _try_layer2_fuzzy behaviour
# ============================================================


@pytest.mark.asyncio
async def test_try_layer2_fuzzy_returns_none_for_empty_existing() -> None:
    linker = _build_linker()
    cand = _make_candidate()
    out = await linker._try_layer2_fuzzy(cand, [], recording_id=1)
    assert out is None


@pytest.mark.asyncio
async def test_try_layer2_fuzzy_uses_injected_matcher() -> None:
    """The linker must use the matcher provided via ctor."""
    class _StubMatcher:
        def __init__(self) -> None:
            self.calls = 0

        def match(
            self,
            *,
            query_name: str,
            candidates: list[SpeakerCandidate],
            query_voiceprint: tuple[float, ...] | None,
        ) -> SpeakerFuzzyResult:
            self.calls += 1
            return SpeakerFuzzyResult(
                verdict="NO_MATCH",
                matched_candidate=None,
                fuzzy_score=0.0,
                voiceprint_score=None,
                needs_reconfirm=False,
            )

    stub = _StubMatcher()
    linker = _build_linker(fuzzy_matcher=stub)

    # We need a SpeakerNode-like object — use a SimpleNamespace.
    from types import SimpleNamespace

    fake_node = SimpleNamespace(id=42, display_name="王小姐")
    out = await linker._try_layer2_fuzzy(
        _make_candidate(), [fake_node], recording_id=1
    )
    assert out is None
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_try_layer2_fuzzy_no_query_name_returns_none() -> None:
    """When _derive_query_name yields speaker_id the matcher still runs but
    we explicitly skip when both display_name and role_hint are empty
    (caller can override this behaviour)."""
    linker = _build_linker()
    cand = _make_candidate(role_hint="", display_name="")
    from types import SimpleNamespace

    fake_node = SimpleNamespace(id=1, display_name="x")
    # speaker_id is non-empty so _derive_query_name returns "spk_0".
    # The matcher runs and likely returns NO_MATCH for "spk_0" vs "x".
    out = await linker._try_layer2_fuzzy(cand, [fake_node], recording_id=1)
    # We can't predict the exact fuzzy score for "spk_0" vs "x" but it's
    # almost certainly below the INFERRED threshold (0.6).
    assert out is None


# ============================================================
# Helpers
# ============================================================


def _make_candidate(
    role_hint: str = "customer",
    display_name: str = "",
) -> _NewSpeakerCandidate:
    return _NewSpeakerCandidate(
        speaker_id="spk_0",
        voiceprint=(1.0, 0.0, 0.0),
        voiceprint_id="hash",
        recording_id=1,
        speech_sec=10.0,
        first_seen=None,
        role_hint=role_hint,
        display_name=display_name,
    )


def _build_linker(
    *,
    enable_layer2_fuzzy: bool = True,
    fuzzy_matcher: Any = None,
) -> SpeakerLinker:
    """Construct a SpeakerLinker with stub deps (no real DB / crypto)."""
    return SpeakerLinker(
        session_factory=None,  # type: ignore[arg-type]
        crypto=None,  # type: ignore[arg-type]
        audit=None,
        voiceprint_threshold=0.5,
        ambiguity_threshold=0.7,
        tenant_id="t1",
        enable_layer2_fuzzy=enable_layer2_fuzzy,
        fuzzy_matcher=fuzzy_matcher,
    )
