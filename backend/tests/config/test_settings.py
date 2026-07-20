"""Unit tests for audio_graphy.config.Settings + build_adapters."""

from __future__ import annotations

import pytest


class TestSettings:
    """Settings loading and validation."""

    @pytest.mark.unit
    def test_defaults_when_no_env(self, fresh_settings) -> None:
        """Default adapter_mode is mock when no env override."""
        assert fresh_settings.adapter_mode == "mock"
        assert fresh_settings.mysql_host == "127.0.0.1"  # from conftest
        assert fresh_settings.default_tenant_id == "default"

    @pytest.mark.unit
    def test_cors_origins_split_correctly(
        self, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORS_ORIGINS", "http://a,http://b, http://c ")
        from audio_graphy.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        assert s.cors_origins_list == ["http://a", "http://b", "http://c"]

    @pytest.mark.unit
    def test_mysql_dsn_async_format(self, fresh_settings) -> None:
        dsn = fresh_settings.mysql_dsn_async
        assert dsn.startswith("mysql+aiomysql://")
        assert "charset=utf8mb4" in dsn

    @pytest.mark.unit
    def test_mysql_dsn_sync_format(self, fresh_settings) -> None:
        dsn = fresh_settings.mysql_dsn_sync
        assert dsn.startswith("mysql+pymysql://")

    @pytest.mark.unit
    def test_working_dir_created_if_missing(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        non_existing = tmp_path / "does_not_exist_yet" / "subdir"
        monkeypatch.setenv("WORKING_DIR", str(non_existing))
        from audio_graphy.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        assert s.working_dir.exists()
        assert s.working_dir.is_dir()

    @pytest.mark.unit
    def test_invalid_mock_error_rate_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MOCK_LLM_ERROR_RATE", "1.5")
        from audio_graphy.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(ValueError, match="MOCK_LLM_ERROR_RATE"):
            get_settings()


class TestBuildAdapters:
    """build_adapters factory."""

    @pytest.mark.unit
    def test_returns_mock_bundle_when_mode_mock(self, fresh_settings) -> None:
        from audio_graphy.adapters.bundle import AdapterBundle
        from audio_graphy.config import build_adapters

        bundle = build_adapters(fresh_settings)
        assert isinstance(bundle, AdapterBundle)
        assert bundle.vad is not None
        assert bundle.asr is not None
        assert bundle.strong_llm is not None
        assert bundle.weak_llm is not None
        assert bundle.embed is not None

    @pytest.mark.unit
    def test_real_mode_raises_not_implemented(
        self, fresh_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADAPTER_MODE", "real")
        from audio_graphy.config import build_adapters, get_settings

        get_settings.cache_clear()
        s = get_settings()
        with pytest.raises(NotImplementedError, match="ADAPTER_MODE=real"):
            build_adapters(s)

    @pytest.mark.unit
    def test_strong_and_weak_llm_have_different_models(self, fresh_settings) -> None:
        from audio_graphy.config import build_adapters

        bundle = build_adapters(fresh_settings)
        assert bundle.strong_llm.model != bundle.weak_llm.model
