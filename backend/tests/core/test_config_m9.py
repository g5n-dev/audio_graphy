"""T15 — M9 config field tests (architecture §15)."""

from __future__ import annotations

import os

import pytest


def _make_settings(**overrides: object) -> object:
    """Construct a Settings instance with WORKING_DIR pointed at /tmp."""
    from audio_graphy.config import Settings

    env = {
        "WORKING_DIR": "/tmp/ag_test_working_dir",
        "JWT_SECRET": "x" * 32,
    }
    env.update({k.upper(): str(v) for k, v in overrides.items()})
    old = os.environ.copy()
    os.environ.update(env)
    try:
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(old)


# ============================================================
# Defaults
# ============================================================


def test_m9_defaults() -> None:
    s = _make_settings()
    assert s.enable_advanced_graph is False  # L9 default
    assert s.enable_bitemporal_edges is True
    assert s.enable_leiden is True
    assert s.leiden_threshold_percent == 30.0  # L2 locked
    assert s.leiden_lib == "networkx"
    assert s.leiden_max_levels == 2  # Q2 cap
    assert s.community_summary_strategy == "eager"
    assert s.enable_compression is True
    assert s.compression_god_node_degree == 50
    assert s.compression_stale_days == 180
    assert s.compression_max_candidates_per_run == 100
    assert s.speaker_fuzzy_ambiguous_threshold == 0.85  # L8
    assert s.speaker_fuzzy_inferred_threshold == 0.6  # L8
    assert s.speaker_fuzzy_voiceprint_reconfirm_cosine == 0.7  # L8
    assert s.enable_speaker_layer2_fuzzy is True


# ============================================================
# Validators
# ============================================================


def test_leiden_threshold_out_of_range_rejected() -> None:
    with pytest.raises(Exception):  # ValidationError
        _make_settings(leiden_threshold_percent=-1.0)
    with pytest.raises(Exception):
        _make_settings(leiden_threshold_percent=150.0)


def test_speaker_fuzzy_thresholds_range_rejected() -> None:
    with pytest.raises(Exception):
        _make_settings(speaker_fuzzy_ambiguous_threshold=-0.1)
    with pytest.raises(Exception):
        _make_settings(speaker_fuzzy_inferred_threshold=1.5)
    with pytest.raises(Exception):
        _make_settings(speaker_fuzzy_voiceprint_reconfirm_cosine=-0.1)


def test_inverted_speaker_fuzzy_thresholds_rejected() -> None:
    """Inferred > ambiguous is invalid (model_validator)."""
    with pytest.raises(Exception):
        _make_settings(
            speaker_fuzzy_inferred_threshold=0.85,
            speaker_fuzzy_ambiguous_threshold=0.6,
        )


def test_leiden_max_levels_range_rejected() -> None:
    with pytest.raises(Exception):
        _make_settings(leiden_max_levels=5)
    with pytest.raises(Exception):
        _make_settings(leiden_max_levels=-1)


def test_master_flag_off_with_subflags_on_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When master flag is off, sub-flags elicit a warning (not error)."""
    import logging

    caplog.set_level(logging.WARNING)
    _make_settings(
        enable_advanced_graph=False,
        enable_bitemporal_edges=True,
        enable_leiden=True,
        enable_compression=True,
    )
    assert any(
        "ENABLE_ADVANCED_GRAPH=False but sub-flags ON" in rec.message
        for rec in caplog.records
    )


def test_master_flag_on_with_subflags_on_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    _make_settings(
        enable_advanced_graph=True,
        enable_bitemporal_edges=True,
        enable_leiden=True,
        enable_compression=True,
    )
    assert not any(
        "ENABLE_ADVANCED_GRAPH=False but sub-flags ON" in rec.message
        for rec in caplog.records
    )
