"""Protocol contract tests for M7 Phase 2 adapters."""

from __future__ import annotations

import pytest

from audio_graphy.adapters import (
    AudioEmbedAdapter,
    AudioEmbeddingResult,
    DiarizationResult,
    DiarizationSegment,
    VoiceprintAdapter,
    VoiceprintResult,
)
from audio_graphy.adapters.mock_audio_embed import MockAudioEmbedAdapter
from audio_graphy.adapters.mock_voiceprint import MockVoiceprintAdapter
from audio_graphy.adapters.real.audio_embed_clap import CLAPServiceAdapter
from audio_graphy.adapters.real.voiceprint_cam import CAMPlusPlusAdapter


class TestAudioEmbedAdapterContract:
    """Mock + Real adapters must satisfy AudioEmbedAdapter Protocol."""

    @pytest.mark.contract
    def test_mock_is_audio_embed_adapter(self) -> None:
        assert isinstance(MockAudioEmbedAdapter(), AudioEmbedAdapter)

    @pytest.mark.contract
    def test_real_is_audio_embed_adapter(self) -> None:
        assert isinstance(CLAPServiceAdapter(url="http://x"), AudioEmbedAdapter)

    @pytest.mark.contract
    def test_audio_embed_has_dim(self) -> None:
        a = MockAudioEmbedAdapter()
        assert a.dim == 512
        assert isinstance(a.model, str)

    @pytest.mark.contract
    def test_audio_embed_has_embed_audio(self) -> None:
        assert callable(getattr(MockAudioEmbedAdapter(), "embed_audio", None))


class TestVoiceprintAdapterContract:
    """Mock + Real adapters must satisfy VoiceprintAdapter Protocol."""

    @pytest.mark.contract
    def test_mock_is_voiceprint_adapter(self) -> None:
        assert isinstance(MockVoiceprintAdapter(), VoiceprintAdapter)

    @pytest.mark.contract
    def test_real_is_voiceprint_adapter(self) -> None:
        assert isinstance(CAMPlusPlusAdapter(url="http://x"), VoiceprintAdapter)

    @pytest.mark.contract
    def test_voiceprint_has_dim(self) -> None:
        a = MockVoiceprintAdapter()
        assert a.dim == 192

    @pytest.mark.contract
    def test_voiceprint_has_diarize(self) -> None:
        assert callable(getattr(MockVoiceprintAdapter(), "diarize", None))

    @pytest.mark.contract
    def test_voiceprint_has_extract_voiceprint(self) -> None:
        assert callable(getattr(MockVoiceprintAdapter(), "extract_voiceprint", None))


class TestPhase2Dataclasses:
    """Frozen dataclasses immutable + slots correct."""

    def test_audio_embedding_result_frozen(self) -> None:
        r = AudioEmbeddingResult(vector=(0.1, 0.2), dim=2, model="x")
        with pytest.raises(AttributeError):
            r.vector = (0.3,)  # type: ignore[misc]

    def test_diarization_segment_required_fields(self) -> None:
        s = DiarizationSegment(start_sec=0.0, end_sec=1.0, speaker_id="spk_0")
        assert s.start_sec == 0.0
        assert s.speaker_id == "spk_0"
        # Unknown by default: funasr rarely reports one, and defaulting to
        # 1.0 would let callers filter on a constant and believe they had
        # filtered on a signal.
        assert s.confidence is None

    def test_diarization_result_defaults(self) -> None:
        r = DiarizationResult(segments=(), num_speakers=0, model="m")
        assert r.duration_sec == 0.0

    def test_voiceprint_result_defaults(self) -> None:
        r = VoiceprintResult(vector=(0.5,), dim=1, model="m")
        assert r.speaker_id == ""
        assert r.duration_sec == 0.0
