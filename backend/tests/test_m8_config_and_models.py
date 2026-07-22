"""M8 Phase 4 — config + ORM + bundle factory tests.

Covers T5 (config streaming fields, StreamingSession ORM, bundle factory).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


class TestStreamingConfig:
    """Verify M8 config fields + validators."""

    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear lru_cache so fresh Settings instance is built.
        from audio_graphy.config import get_settings

        # Strip any ENABLE_STREAMING from env.
        monkeypatch.delenv("ENABLE_STREAMING", raising=False)
        get_settings.cache_clear()
        s = get_settings()
        assert s.enable_streaming is False
        assert s.enable_streaming_retrieval is False
        assert s.adapter_streaming_vad_mode == "mock"
        assert s.adapter_streaming_asr_mode == "mock"
        assert s.streaming_vad_onset_threshold == 0.5
        assert s.streaming_vad_offset_threshold == 0.35
        assert s.streaming_vad_min_speech_sec == 0.25
        assert s.streaming_vad_min_silence_sec == 0.10
        assert s.streaming_vad_chunk_samples == 512
        assert s.streaming_vad_reset_seq_gap == 3
        assert s.streaming_asr_pool_size_per_tenant == 8
        assert s.streaming_tag_interval == 5
        assert s.streaming_session_timeout_sec == 300.0
        assert s.streaming_ambiguous_edge_weight == 0.5
        assert s.streaming_inferred_edge_weight == 0.8
        assert s.ws_jwt_ttl_minutes == 5
        get_settings.cache_clear()

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "true")
        monkeypatch.setenv("STREAMING_VAD_ONSET_THRESHOLD", "0.7")
        monkeypatch.setenv("STREAMING_ASR_POOL_SIZE_PER_TENANT", "16")
        monkeypatch.setenv("STREAMING_AMBIGUOUS_EDGE_WEIGHT", "0.3")
        get_settings.cache_clear()
        s = get_settings()
        assert s.enable_streaming is True
        assert s.streaming_vad_onset_threshold == 0.7
        assert s.streaming_asr_pool_size_per_tenant == 16
        assert s.streaming_ambiguous_edge_weight == 0.3
        get_settings.cache_clear()

    def test_invalid_onset_threshold_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.config import Settings

        monkeypatch.setenv("STREAMING_VAD_ONSET_THRESHOLD", "1.5")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_ambiguous_weight_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.config import Settings

        monkeypatch.setenv("STREAMING_AMBIGUOUS_EDGE_WEIGHT", "-0.5")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_seq_gap_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.config import Settings

        monkeypatch.setenv("STREAMING_VAD_RESET_SEQ_GAP", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_pool_size_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.config import Settings

        monkeypatch.setenv("STREAMING_ASR_POOL_SIZE_PER_TENANT", "0")
        with pytest.raises(ValidationError):
            Settings()


class TestStreamingBundleFactory:
    """Verify build_streaming_adapters + per-session factories."""

    def test_disabled_returns_empty_bundle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.adapters.bundle import build_streaming_adapters
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "false")
        get_settings.cache_clear()
        s = get_settings()
        bundle = build_streaming_adapters(s)
        assert bundle.vad is None
        assert bundle.asr is None
        assert bundle.pool is None
        get_settings.cache_clear()

    def test_mock_mode_returns_mock_adapters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.adapters.bundle import build_streaming_adapters
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "true")
        monkeypatch.setenv("ADAPTER_STREAMING_VAD_MODE", "mock")
        monkeypatch.setenv("ADAPTER_STREAMING_ASR_MODE", "mock")
        get_settings.cache_clear()
        s = get_settings()
        bundle = build_streaming_adapters(s)
        assert isinstance(bundle.vad, MockStreamingVADAdapter)
        assert isinstance(bundle.asr, MockStreamingASRAdapter)
        assert bundle.pool is None  # no pool in mock mode
        get_settings.cache_clear()

    def test_per_session_factories_build_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.adapters.bundle import (
            build_streaming_adapters_for_session,
        )
        from audio_graphy.adapters.mock_streaming_asr import MockStreamingASRAdapter
        from audio_graphy.adapters.mock_streaming_vad import MockStreamingVADAdapter
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "true")
        monkeypatch.setenv("ADAPTER_STREAMING_VAD_MODE", "mock")
        monkeypatch.setenv("ADAPTER_STREAMING_ASR_MODE", "mock")
        get_settings.cache_clear()
        s = get_settings()
        pair = build_streaming_adapters_for_session(s)
        assert isinstance(pair.vad, MockStreamingVADAdapter)
        assert isinstance(pair.asr, MockStreamingASRAdapter)
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_acquire_per_session_mock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from audio_graphy.adapters.bundle import (
            acquire_streaming_adapters_for_session,
        )
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "true")
        monkeypatch.setenv("ADAPTER_STREAMING_VAD_MODE", "mock")
        monkeypatch.setenv("ADAPTER_STREAMING_ASR_MODE", "mock")
        get_settings.cache_clear()
        s = get_settings()
        pair = await acquire_streaming_adapters_for_session(
            s, tenant_id="t1", session_id="s1", hotwords=(), pool=None,
        )
        assert pair.vad is not None
        assert pair.asr is not None
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_acquire_real_without_pool_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from audio_graphy.adapters.bundle import (
            acquire_streaming_adapters_for_session,
        )
        from audio_graphy.config import get_settings

        monkeypatch.setenv("ENABLE_STREAMING", "true")
        monkeypatch.setenv("ADAPTER_STREAMING_VAD_MODE", "mock")
        monkeypatch.setenv("ADAPTER_STREAMING_ASR_MODE", "real")
        get_settings.cache_clear()
        s = get_settings()
        with pytest.raises(RuntimeError, match="FunASRConnectionPool"):
            await acquire_streaming_adapters_for_session(
                s, tenant_id="t1", session_id="s1", hotwords=(), pool=None,
            )
        get_settings.cache_clear()


# ============================================================
# T5 — StreamingSession ORM model
# ============================================================


class TestStreamingSessionORM:
    """Verify the ORM model is correctly defined."""

    def test_table_name(self) -> None:
        from audio_graphy.models.streaming_session import StreamingSession

        assert StreamingSession.__tablename__ == "streaming_sessions"

    def test_columns_present(self) -> None:
        from audio_graphy.models.streaming_session import StreamingSession

        cols = {c.name for c in StreamingSession.__table__.columns}
        expected = {
            "id", "tenant_id", "session_id", "recording_id",
            "user_id", "started_at", "ended_at", "last_chunk_at",
            "seg_confirmed_count", "seg_realtime_count", "bytes_in",
            "error_count", "end_reason", "consent_token_hash",
            "stats", "created_at", "updated_at",
        }
        assert expected.issubset(cols)

    def test_unique_constraint_on_session_id(self) -> None:
        from audio_graphy.models.streaming_session import StreamingSession

        constraint_names = [
            c.name for c in StreamingSession.__table__.constraints
            if hasattr(c, "name") and c.name and "ux" in c.name
        ]
        assert any("session_id" in n for n in constraint_names)

    def test_check_constraint_on_end_reason(self) -> None:
        from audio_graphy.models.streaming_session import StreamingSession

        check_sql = " ".join(
            str(c.sqltext).lower() if hasattr(c, "sqltext") else ""
            for c in StreamingSession.__table__.constraints
        )
        assert "end_reason" in check_sql
        assert "normal" in check_sql
        assert "backpressure" in check_sql

    def test_indexes_present(self) -> None:
        from audio_graphy.models.streaming_session import StreamingSession

        index_names = {idx.name for idx in StreamingSession.__table__.indexes}
        assert "ix_streaming_sessions_tenant_started" in index_names
        assert "ix_streaming_sessions_recording" in index_names


# ============================================================
# T5 — Alembic migration file (static check)
# ============================================================


class TestAlembicMigration:
    """Verify the migration file is well-formed."""

    def test_revision_id(self) -> None:
        # Parse the migration file as text to avoid `from alembic import op`
        # which only works at alembic runtime.
        from pathlib import Path

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic" / "versions" / "0009_m8_streaming_init.py"
        )
        source = migration_path.read_text(encoding="utf-8")

        # Extract revision/down_revision via regex (handles `revision: str = "..."` form).
        import re

        rev_match = re.search(
            r"^revision\s*(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE,
        )
        down_match = re.search(
            r"^down_revision\s*(?::\s*str\s*\|\s*None)?\s*=\s*['\"]([^'\"]+)['\"]",
            source, re.MULTILINE,
        )
        assert rev_match is not None, "revision = '...' not found"
        assert down_match is not None, "down_revision = '...' not found"
        assert rev_match.group(1) == "0009_m8_streaming_init"
        assert down_match.group(1) == "0008_m7_indexes"

        # Verify upgrade/downgrade defs exist.
        assert "def upgrade()" in source
        assert "def downgrade()" in source
        # Verify streaming_sessions table creation is present.
        assert "streaming_sessions" in source
        assert "create_table" in source

    def test_upgrade_downgrade_callable(self) -> None:
        # Stub out alembic.op + sa so the migration module can be imported.
        import importlib
        import sys
        import types
        from pathlib import Path

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic" / "versions" / "0009_m8_streaming_init.py"
        )

        # Inject stub alembic module with op.
        if "alembic" not in sys.modules or not hasattr(sys.modules["alembic"], "op"):
            fake_alembic = types.ModuleType("alembic")
            fake_op = types.ModuleType("alembic.op")
            fake_op.create_table = lambda *a, **kw: None
            fake_op.drop_table = lambda *a, **kw: None
            fake_op.create_index = lambda *a, **kw: None
            fake_op.drop_index = lambda *a, **kw: None
            fake_alembic.op = fake_op
            sys.modules["alembic"] = fake_alembic
            sys.modules["alembic.op"] = fake_op

        spec = importlib.util.spec_from_file_location(
            "m8_streaming_init_check", migration_path,
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)
