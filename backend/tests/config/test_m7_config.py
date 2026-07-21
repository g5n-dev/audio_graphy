"""Unit tests for M7 config additions (Settings).

Covers new fields + validators:
- ``adapter_audio_embed_mode`` / ``adapter_voiceprint_mode`` default mock.
- ``clap_service_url`` / ``campplus_service_url`` defaults.
- ``voiceprint_cosine_threshold`` / ``voiceprint_ambiguous_threshold`` range.
- Cross-field validator (cosine ≤ ambiguous).
- ``clap_force_gpu`` / ``campplus_prefer_gpu`` / ``voiceprint_retention_cascade``.
- ``rerank_channel_weights`` (Q1 locked) sum-to-1 validator.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_graphy.config import Settings


def _base_kwargs() -> dict:
    """Minimal kwargs to instantiate Settings without env interference."""
    return {
        "working_dir": "/tmp/ag_test",
        "master_key_path": "/tmp/ag_test/master.key",
    }


class TestDefaults:
    def test_voiceprint_default_off(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.enable_voiceprint is False
        assert s.enable_clap is False

    def test_audio_embed_mode_default_mock(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.adapter_audio_embed_mode == "mock"

    def test_voiceprint_mode_default_mock(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.adapter_voiceprint_mode == "mock"

    def test_clap_service_url_default(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.clap_service_url == "http://clap-service:8006"

    def test_campplus_service_url_default(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.campplus_service_url == "http://campplus-service:8007"

    def test_voiceprint_thresholds_defaults(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.voiceprint_cosine_threshold == 0.5
        assert s.voiceprint_ambiguous_threshold == 0.7

    def test_gpu_flags_defaults(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.clap_force_gpu is True
        assert s.campplus_prefer_gpu is False

    def test_voiceprint_retention_cascade_default(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.voiceprint_retention_cascade is True

    def test_rerank_channel_weights_q1_locked(self, tmp_path) -> None:
        """Q1 decision: rerank weights = (0.5, 0.3, 0.2)."""
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.rerank_channel_weights == (0.5, 0.3, 0.2)


class TestValidators:
    def test_vp_cosine_threshold_below_zero_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_cosine_threshold=-0.01,
            )

    def test_vp_cosine_threshold_above_one_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_cosine_threshold=1.01,
            )

    def test_vp_ambiguous_threshold_below_zero_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_ambiguous_threshold=-0.5,
            )

    def test_vp_ambiguous_threshold_above_one_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_ambiguous_threshold=2.0,
            )

    def test_cosine_greater_than_ambiguous_rejected(self, tmp_path) -> None:
        """Cross-field: cosine must be ≤ ambiguous (model_validator)."""
        with pytest.raises(ValidationError, match="VOICEPRINT_COSINE_THRESHOLD"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_cosine_threshold=0.9,
                voiceprint_ambiguous_threshold=0.5,
            )

    def test_cosine_equal_ambiguous_allowed(self, tmp_path) -> None:
        s = Settings(
            working_dir=str(tmp_path),
            master_key_path=str(tmp_path / "k"),
            voiceprint_cosine_threshold=0.7,
            voiceprint_ambiguous_threshold=0.7,
        )
        assert s.voiceprint_cosine_threshold == 0.7


class TestRerankWeightsValidator:
    def test_sum_below_one_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError, match=r"sum to 1.0"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                rerank_channel_weights=(0.3, 0.3, 0.3),  # sum=0.9
            )

    def test_sum_above_one_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError, match=r"sum to 1.0"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                rerank_channel_weights=(0.5, 0.5, 0.5),  # sum=1.5
            )

    def test_negative_weight_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError, match=r"in \[0, 1\]"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                rerank_channel_weights=(-0.1, 0.5, 0.6),
            )

    def test_valid_alternate_weights_accepted(self, tmp_path) -> None:
        """Weights other than the default are allowed as long as they sum to 1."""
        s = Settings(
            working_dir=str(tmp_path),
            master_key_path=str(tmp_path / "k"),
            rerank_channel_weights=(0.6, 0.2, 0.2),
        )
        assert s.rerank_channel_weights == (0.6, 0.2, 0.2)
