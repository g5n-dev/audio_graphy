"""API tests for GET /speakers/voiceprint-policy.

Coverage:
    - Happy path for viewer / inspector / admin (viewer+ read access).
    - Agent role forbidden (403).
    - Unauthenticated forbidden (401).
    - Response mirrors app settings (thresholds + flags).
    - No biometric data in the response.
    - Literal path is not shadowed by GET /speakers/{speaker_id}.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.integration


class TestVoiceprintPolicy:
    """GET /api/v1/speakers/voiceprint-policy."""

    def test_viewer_can_read_policy(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        resp = test_client.get(
            "/api/v1/speakers/voiceprint-policy",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        settings = test_client.app.state.settings
        assert body["enable_voiceprint"] == settings.enable_voiceprint
        assert body["adapter_voiceprint_mode"] == settings.adapter_voiceprint_mode
        assert body["layer1"] == {
            "cosine_threshold": settings.voiceprint_cosine_threshold,
            "ambiguous_threshold": settings.voiceprint_ambiguous_threshold,
        }
        assert body["layer2"] == {
            "enabled": settings.enable_speaker_layer2_fuzzy,
            "fuzzy_inferred_threshold": settings.speaker_fuzzy_inferred_threshold,
            "fuzzy_ambiguous_threshold": settings.speaker_fuzzy_ambiguous_threshold,
            "voiceprint_reconfirm_cosine": (settings.speaker_fuzzy_voiceprint_reconfirm_cosine),
        }
        # Sampling must mirror the adapter-shared constants, not a literal
        # copy — otherwise the drawer can drift away from the real pipeline.
        from audio_graphy.adapters.protocols import (
            DEFAULT_MAX_SPEAKERS,
            DEFAULT_MIN_SEGMENT_SEC,
            VOICEPRINT_DIM,
        )

        assert body["sampling"] == {
            "strategy": settings.voiceprint_sampling_strategy,
            "min_segment_sec": settings.voiceprint_sample_min_segment_sec,
            "min_total_sec": settings.voiceprint_sample_min_total_sec,
            "max_segments_per_speaker": settings.voiceprint_sample_max_segments,
            "diarization_min_segment_sec": DEFAULT_MIN_SEGMENT_SEC,
            "max_speakers": DEFAULT_MAX_SPEAKERS,
            "embedding_dim": VOICEPRINT_DIM,
        }
        assert body["retention_cascade"] == settings.voiceprint_retention_cascade

    @pytest.mark.parametrize("role_key", ["inspector_t1", "admin_t1"])
    def test_inspector_and_admin_can_read_policy(
        self,
        test_client: Any,
        auth_headers: Any,
        role_key: str,
    ) -> None:
        resp = test_client.get(
            "/api/v1/speakers/voiceprint-policy",
            headers=auth_headers[role_key],
        )
        assert resp.status_code == 200

    def test_agent_forbidden(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        resp = test_client.get(
            "/api/v1/speakers/voiceprint-policy",
            headers=auth_headers["agent_t1"],
        )
        assert resp.status_code == 403

    def test_unauthenticated_rejected(
        self,
        test_client: Any,
    ) -> None:
        resp = test_client.get("/api/v1/speakers/voiceprint-policy")
        assert resp.status_code == 401

    def test_no_biometric_payload(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Policy is settings-only — never vectors or hashes."""
        resp = test_client.get(
            "/api/v1/speakers/voiceprint-policy",
            headers=auth_headers["inspector_t1"],
        )
        body = resp.json()
        assert set(body.keys()) == {
            "enable_voiceprint",
            "adapter_voiceprint_mode",
            "layer1",
            "layer2",
            "sampling",
            "retention_cascade",
        }

    def test_literal_path_not_shadowed_by_speaker_id(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """The literal route must win over /speakers/{speaker_id} (int coercion)."""
        resp = test_client.get(
            "/api/v1/speakers/voiceprint-policy",
            headers=auth_headers["inspector_t1"],
        )
        # A shadowed route would 422 on int("voiceprint-policy").
        assert resp.status_code == 200
