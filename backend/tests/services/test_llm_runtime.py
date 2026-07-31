"""Lifecycle and restart contracts for the production LLM runtime wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.config import Settings, build_adapters
from audio_graphy.models.base import Base
from audio_graphy.services.llm_gateway import LLMGateway, LLMProvenance, LLMRequest
from audio_graphy.services.llm_runtime import build_llm_runtime


@pytest_asyncio.fixture
async def runtime_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(Fernet.generate_key())
    return Settings(
        working_dir=tmp_path / "work",
        master_key_path=str(key_path),
        redis_url=None,
        mock_llm_error_rate=0,
        llm_cache_cleanup_interval_seconds=3600,
        **overrides,
    )


async def test_runtime_injects_distinct_immutable_tier_price_snapshots(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        llm_price_version="provider-price-2026-07",
        llm_strong_input_microunits_per_million_tokens=2_000_000,
        llm_strong_output_microunits_per_million_tokens=8_000_000,
        llm_strong_cached_prefill_microunits_per_million_tokens=500_000,
        llm_weak_input_microunits_per_million_tokens=500_000,
        llm_weak_output_microunits_per_million_tokens=2_000_000,
        llm_weak_cached_prefill_microunits_per_million_tokens=125_000,
    )
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    strong = runtime.bundle.strong_llm
    weak = runtime.bundle.weak_llm
    assert isinstance(strong, LLMGateway)
    assert isinstance(weak, LLMGateway)
    assert strong.price_snapshot is not None
    assert weak.price_snapshot is not None
    assert strong.price_snapshot.version == "provider-price-2026-07"
    assert (
        strong.price_snapshot.input_microunits_per_million_tokens
        == 2_000_000
    )
    assert weak.price_snapshot.input_microunits_per_million_tokens == 500_000
    await runtime.aclose()


def _request() -> LLMRequest:
    return LLMRequest(
        tenant_id="tenant-a",
        purpose="runtime_test",
        model_tier="weak",
        messages=({"role": "user", "content": "hello"},),
        model_epoch="epoch-1",
        prompt_version="v1",
        parser_version="v1",
        business_snapshot={"source_sha256": "f" * 64},
        ttl_seconds=60,
    )


async def test_runtime_wraps_both_tiers_and_reuses_mysql_after_restart(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_raw = build_adapters(settings)
    first_runtime = await build_llm_runtime(settings, runtime_factory, first_raw)

    assert isinstance(first_runtime.bundle.strong_llm, LLMGateway)
    assert isinstance(first_runtime.bundle.weak_llm, LLMGateway)
    assert first_runtime.cache.backend_name == "local"
    first = await first_runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    assert first.provider_called
    await first_runtime.aclose()

    # New adapters + empty process-local cache, same durable database.
    second_raw = build_adapters(settings)
    second_runtime = await build_llm_runtime(settings, runtime_factory, second_raw)
    second = await second_runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    assert second.cached
    assert second.cache_source == "mysql"
    assert not second.provider_called
    await second_runtime.aclose()


@pytest.mark.parametrize("disabled_setting", ({"enable_llm_exact_cache": False},))
async def test_runtime_rollout_switches_bypass_result_reuse(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    disabled_setting: dict[str, bool],
) -> None:
    settings = _settings(tmp_path).model_copy(update=disabled_setting)
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    first = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    second = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]

    assert first.provider_called
    assert second.provider_called
    assert first.prompt_hash == second.prompt_hash
    await runtime.aclose()


async def test_recipe_shadow_mode_keeps_v1_cache_authoritative_while_probing_v2(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"llm_recipe_shadow_mode": True})
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    first = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    second = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]

    assert first.provider_called
    assert second.cached
    assert not second.provider_called
    await runtime.aclose()


@pytest.mark.parametrize("mode", ("shadow", "dual_read", "v2"))
async def test_runtime_passes_explicit_recipe_migration_mode_to_both_gateways(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: str,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"llm_recipe_migration_mode": mode}
    )
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    assert runtime.bundle.strong_llm._recipe_migration_mode == mode  # type: ignore[attr-defined]
    assert runtime.bundle.weak_llm._recipe_migration_mode == mode  # type: ignore[attr-defined]
    await runtime.aclose()


async def test_persistent_switch_can_fall_back_to_hot_only_reuse(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"enable_llm_persistent_cache": False})
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    first = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    second = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]

    assert first.provider_called
    assert second.cached
    assert second.cache_source == "local"
    assert not second.provider_called
    await runtime.aclose()


async def test_hot_only_mode_bypasses_provenance_values(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"enable_llm_persistent_cache": False})
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )
    request = replace(
        _request(),
        provenance=(LLMProvenance(source_type="recording", source_id="recording-1"),),
    )

    first = await runtime.bundle.weak_llm.execute(request)  # type: ignore[attr-defined]
    second = await runtime.bundle.weak_llm.execute(request)  # type: ignore[attr-defined]

    assert first.provider_called
    assert second.provider_called
    await runtime.aclose()


async def test_hot_cache_switch_keeps_mysql_exact_cache_enabled(
    runtime_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(update={"enable_llm_hot_cache": False})
    runtime = await build_llm_runtime(
        settings,
        runtime_factory,
        build_adapters(settings),
    )

    first = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]
    second = await runtime.bundle.weak_llm.execute(_request())  # type: ignore[attr-defined]

    assert runtime.cache.backend_name == "disabled"
    assert first.provider_called
    assert second.cached
    assert second.cache_source == "mysql"
    assert not second.provider_called
    await runtime.aclose()
