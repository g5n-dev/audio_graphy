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
    def test_voiceprint_and_clap_both_default_off(self, tmp_path) -> None:
        """Neither identity feature ships enabled.

        Speaker linking briefly defaulted on while ADAPTER_VOICEPRINT_MODE still
        defaulted to mock. The mock adapter derives vectors from the diarization
        label, so the same label in unrelated recordings matches above the
        unambiguous-merge threshold: the shipped default silently merged
        distinct speakers into one identity and stored it as encrypted biometric
        data. Turning it on is now an explicit choice made alongside a real
        adapter.
        """
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.enable_voiceprint is False
        assert s.enable_clap is False

    def test_voiceprint_on_mock_adapter_warns(self, tmp_path, caplog) -> None:
        """The dangerous combination is allowed but never silent.

        Mock-chain tests rely on it, so it cannot be an error — but a deployment
        that lands here is producing confident nonsense rather than degraded
        output, and must be told so.
        """
        with caplog.at_level("WARNING", logger="audio_graphy.config"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                enable_voiceprint=True,
                adapter_voiceprint_mode="mock",
            )
        assert any("mock voiceprints" in r.message for r in caplog.records)

    def test_voiceprint_on_real_adapter_is_quiet(self, tmp_path, caplog) -> None:
        """The supported combination must not train operators to ignore the warning."""
        with caplog.at_level("WARNING", logger="audio_graphy.config"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                enable_voiceprint=True,
                adapter_voiceprint_mode="real",
            )
        assert not any("mock voiceprints" in r.message for r in caplog.records)

    def test_voiceprint_can_be_turned_off(self, tmp_path) -> None:
        """The M3-M6 escape hatch must still work."""
        s = Settings(
            working_dir=str(tmp_path),
            master_key_path=str(tmp_path / "k"),
            enable_voiceprint=False,
        )
        assert s.enable_voiceprint is False

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


class TestVoiceprintSamplingConfig:
    """ADR-0001 — sampling strategy + quality gates."""

    def test_defaults(self, tmp_path) -> None:
        s = Settings(working_dir=str(tmp_path), master_key_path=str(tmp_path / "k"))
        assert s.voiceprint_sampling_strategy == "weighted_mean"
        assert s.voiceprint_sample_min_segment_sec == 1.0
        assert s.voiceprint_sample_min_total_sec == 3.0
        assert s.voiceprint_sample_max_segments == 8
        assert s.voiceprint_sample_outlier_cosine == 0.5

    def test_longest_segment_strategy_accepted(self, tmp_path) -> None:
        s = Settings(
            working_dir=str(tmp_path),
            master_key_path=str(tmp_path / "k"),
            voiceprint_sampling_strategy="longest_segment",
        )
        assert s.voiceprint_sampling_strategy == "longest_segment"

    def test_unknown_strategy_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_sampling_strategy="merged_reception_audio",
            )

    def test_segment_floor_above_total_floor_rejected(self, tmp_path) -> None:
        """Otherwise no speaker could ever clear the total-speech gate."""
        with pytest.raises(ValidationError, match=r"MIN_SEGMENT_SEC must be"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_sample_min_segment_sec=5.0,
                voiceprint_sample_min_total_sec=3.0,
            )

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_non_positive_durations_rejected(self, tmp_path, value: float) -> None:
        with pytest.raises(ValidationError, match=r"must be > 0"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_sample_min_segment_sec=value,
            )

    def test_zero_max_segments_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError, match=r"MAX_SEGMENTS must be"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_sample_max_segments=0,
            )

    def test_outlier_cosine_out_of_range_rejected(self, tmp_path) -> None:
        with pytest.raises(ValidationError, match=r"OUTLIER_COSINE must be"):
            Settings(
                working_dir=str(tmp_path),
                master_key_path=str(tmp_path / "k"),
                voiceprint_sample_outlier_cosine=1.5,
            )

    def test_outlier_rejection_can_be_disabled(self, tmp_path) -> None:
        s = Settings(
            working_dir=str(tmp_path),
            master_key_path=str(tmp_path / "k"),
            voiceprint_sample_outlier_cosine=0.0,
        )
        assert s.voiceprint_sample_outlier_cosine == 0.0
