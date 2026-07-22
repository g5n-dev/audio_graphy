"""Coverage tests for adapter bundle factories.

Targets the various flag combinations in ``build_hybrid_bundle`` and
``build_streaming_adapters`` that the existing tests don't reach.
"""

from __future__ import annotations

import pytest

from audio_graphy.adapters.bundle import (
    StreamingAdapterBundle,
    build_mock_bundle,
    build_streaming_adapters,
    build_streaming_asr_for_session,
    build_streaming_vad_for_session,
)
from audio_graphy.config import Settings


def _make_settings(**overrides: object) -> Settings:
    """Build a Settings instance with the given overrides applied."""
    base = Settings()
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ============================================================
# build_mock_bundle
# ============================================================


def test_build_mock_bundle_defaults_all_none_audio_voiceprint():
    """With enable_clap=False and enable_voiceprint=False, audio/voiceprint are None."""
    s = _make_settings(enable_clap=False, enable_voiceprint=False)
    b = build_mock_bundle(s)
    assert b.audio_embed is None
    assert b.voiceprint is None
    # Sanity: core adapters always populated.
    assert b.vad is not None
    assert b.asr is not None
    assert b.strong_llm is not None
    assert b.weak_llm is not None
    assert b.embed is not None


def test_build_mock_bundle_enables_clap_when_flag_on():
    """With enable_clap=True, audio_embed is a MockAudioEmbedAdapter."""
    s = _make_settings(enable_clap=True, enable_voiceprint=False)
    b = build_mock_bundle(s)
    assert b.audio_embed is not None
    assert b.voiceprint is None


def test_build_mock_bundle_enables_voiceprint_when_flag_on():
    """With enable_voiceprint=True, voiceprint is a MockVoiceprintAdapter."""
    s = _make_settings(enable_clap=False, enable_voiceprint=True)
    b = build_mock_bundle(s)
    assert b.voiceprint is not None
    assert b.audio_embed is None


def test_build_mock_bundle_enables_both_flags():
    """Both enable_clap + enable_voiceprint → both adapters populated."""
    s = _make_settings(enable_clap=True, enable_voiceprint=True)
    b = build_mock_bundle(s)
    assert b.audio_embed is not None
    assert b.voiceprint is not None


# ============================================================
# build_streaming_adapters — disabled + mock mode
# ============================================================


def test_build_streaming_adapters_disabled_returns_empty_bundle():
    """When enable_streaming=False, all bundle fields are None."""
    s = _make_settings(enable_streaming=False)
    bundle = build_streaming_adapters(s)
    assert isinstance(bundle, StreamingAdapterBundle)
    assert bundle.vad is None
    assert bundle.asr is None
    assert bundle.pool is None


def test_build_streaming_adapters_mock_mode():
    """Mock streaming mode returns Mock adapters and no pool."""
    s = _make_settings(
        enable_streaming=True,
        adapter_streaming_vad_mode="mock",
        adapter_streaming_asr_mode="mock",
    )
    bundle = build_streaming_adapters(s)
    assert bundle.vad is not None
    assert bundle.asr is not None
    assert bundle.pool is None  # pool only for real mode


# ============================================================
# build_streaming_vad_for_session
# ============================================================


def test_build_streaming_vad_for_session_mock():
    s = _make_settings(adapter_streaming_vad_mode="mock")
    vad = build_streaming_vad_for_session(s)
    assert vad is not None


# ============================================================
# build_streaming_asr_for_session
# ============================================================


def test_build_streaming_asr_for_session_mock_returns_adapter():
    s = _make_settings(adapter_streaming_asr_mode="mock")
    asr = build_streaming_asr_for_session(s)
    assert asr is not None


def test_build_streaming_asr_for_session_real_raises():
    """Real mode raises RuntimeError (caller must use the pool instead)."""
    s = _make_settings(adapter_streaming_asr_mode="real")
    with pytest.raises(RuntimeError, match="mock mode only"):
        build_streaming_asr_for_session(s)
