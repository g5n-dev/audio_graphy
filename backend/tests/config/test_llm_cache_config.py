"""Configuration bounds for the optional Redis and persistent LLM cache."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_graphy.config import Settings


def test_llm_cache_defaults_are_bounded_and_local_without_redis(tmp_path) -> None:
    settings = Settings(working_dir=tmp_path, redis_url=None)

    assert settings.llm_hot_cache_backend == "auto"
    assert settings.redis_url is None
    assert settings.llm_local_cache_max_entries == 1024
    assert settings.llm_local_cache_max_bytes == 32 * 1024 * 1024
    assert settings.llm_hot_cache_max_item_bytes == 1024 * 1024
    assert settings.llm_local_cache_ttl_seconds <= 300
    assert settings.llm_redis_cache_ttl_seconds <= 3600
    assert settings.llm_cache_max_entries_per_tenant == 50_000
    assert settings.llm_cache_max_bytes_per_tenant == 256 * 1024 * 1024
    assert not settings.llm_recipe_shadow_mode
    assert settings.llm_recipe_migration_mode == "dual_read"
    assert settings.llm_recipe_migration_mode_resolved == "dual_read"
    assert settings.enable_llm_exact_cache
    assert settings.enable_llm_hot_cache
    assert settings.enable_llm_persistent_cache
    assert not settings.enable_llm_semantic_cache
    assert not settings.enable_llm_batch_judge
    assert settings.enable_hybrid_rule_short_circuit
    assert not settings.enable_adaptive_gleaning
    assert settings.llm_strong_concurrency == 4
    assert settings.llm_weak_concurrency == 8
    assert settings.llm_strong_model_epoch == ""
    assert settings.llm_weak_model_epoch == ""
    assert settings.llm_strong_structured_output_capability == "strict_json_schema"
    assert settings.llm_weak_structured_output_capability == "strict_json_schema"
    assert settings.llm_price_version == ""
    assert settings.llm_strong_input_microunits_per_million_tokens is None
    assert settings.llm_weak_output_microunits_per_million_tokens is None


def test_llm_price_snapshot_is_explicit_complete_and_bounded(tmp_path) -> None:
    settings = Settings(
        working_dir=tmp_path,
        llm_price_version=" provider-price-2026-07 ",
        llm_strong_input_microunits_per_million_tokens=2_000_000,
        llm_strong_output_microunits_per_million_tokens=8_000_000,
        llm_strong_cached_prefill_microunits_per_million_tokens=500_000,
        llm_weak_input_microunits_per_million_tokens=500_000,
        llm_weak_output_microunits_per_million_tokens=2_000_000,
        llm_weak_cached_prefill_microunits_per_million_tokens=125_000,
    )

    assert settings.llm_price_version == "provider-price-2026-07"

    with pytest.raises(ValidationError, match="all-or-none"):
        Settings(
            working_dir=tmp_path,
            llm_price_version="provider-price-2026-07",
            llm_strong_input_microunits_per_million_tokens=2_000_000,
        )
    with pytest.raises(ValidationError, match="configured together"):
        Settings(
            working_dir=tmp_path,
            llm_price_version="provider-price-2026-07",
        )
    with pytest.raises(ValidationError, match="cached-prefill"):
        Settings(
            working_dir=tmp_path,
            llm_price_version="provider-price-2026-07",
            llm_strong_input_microunits_per_million_tokens=2,
            llm_strong_output_microunits_per_million_tokens=8,
            llm_strong_cached_prefill_microunits_per_million_tokens=3,
            llm_weak_input_microunits_per_million_tokens=2,
            llm_weak_output_microunits_per_million_tokens=8,
            llm_weak_cached_prefill_microunits_per_million_tokens=1,
        )


@pytest.mark.parametrize("mode", ("shadow", "dual_read", "v2"))
def test_recipe_migration_mode_accepts_all_explicit_states(tmp_path, mode: str) -> None:
    settings = Settings(working_dir=tmp_path, llm_recipe_migration_mode=mode)

    assert settings.llm_recipe_migration_mode_resolved == mode


@pytest.mark.parametrize(
    ("legacy_shadow", "expected"),
    ((True, "shadow"), (False, "dual_read")),
)
def test_legacy_recipe_shadow_bool_remains_compatible(
    tmp_path,
    legacy_shadow: bool,
    expected: str,
) -> None:
    settings = Settings(working_dir=tmp_path, llm_recipe_shadow_mode=legacy_shadow)

    assert settings.llm_recipe_migration_mode_resolved == expected


def test_explicit_recipe_mode_wins_over_legacy_compatibility_flag(tmp_path) -> None:
    settings = Settings(
        working_dir=tmp_path,
        llm_recipe_migration_mode="v2",
        llm_recipe_shadow_mode=True,
    )

    assert settings.llm_recipe_migration_mode_resolved == "v2"


@pytest.mark.parametrize(
    "field",
    (
        "llm_strong_structured_output_capability",
        "llm_weak_structured_output_capability",
    ),
)
def test_structured_output_capability_rejects_unknown_values(
    tmp_path,
    field: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(working_dir=tmp_path, **{field: "silently_guess"})


def test_model_epochs_can_be_bumped_without_renaming_served_models(tmp_path) -> None:
    settings = Settings(
        working_dir=tmp_path,
        llm_strong_model="served-strong",
        llm_weak_model="served-weak",
        llm_strong_model_epoch="weights-2026-07-25",
        llm_weak_model_epoch="weights-2026-07-20",
    )

    assert settings.llm_strong_model == "served-strong"
    assert settings.llm_strong_model_epoch == "weights-2026-07-25"
    assert settings.llm_weak_model == "served-weak"
    assert settings.llm_weak_model_epoch == "weights-2026-07-20"


def test_explicit_redis_requires_a_valid_secret_url(tmp_path) -> None:
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(
            working_dir=tmp_path,
            llm_hot_cache_backend="redis",
            redis_url=None,
        )
    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings(
            working_dir=tmp_path,
            llm_hot_cache_backend="redis",
            redis_url="https://not-redis.example",
        )

    settings = Settings(
        working_dir=tmp_path,
        llm_hot_cache_backend="redis",
        redis_url="redis://:password@redis:6379/0",
    )
    assert settings.redis_url is not None
    assert "password" not in repr(settings.redis_url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_local_cache_max_entries", 0),
        ("llm_local_cache_max_bytes", 0),
        ("llm_hot_cache_max_item_bytes", 0),
        ("llm_local_cache_ttl_seconds", 0),
        ("llm_redis_cache_ttl_seconds", 0),
        ("llm_redis_failure_threshold", 0),
        ("llm_redis_circuit_seconds", 0),
        ("llm_redis_recovery_successes", 0),
        ("llm_cache_lease_seconds", 0),
        ("llm_cache_cleanup_batch_size", 0),
        ("llm_cache_max_entries_per_tenant", 0),
        ("llm_cache_max_bytes_per_tenant", 0),
        ("llm_strong_concurrency", 0),
        ("llm_weak_concurrency", 0),
    ],
)
def test_llm_cache_resource_limits_must_be_positive(tmp_path, field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(working_dir=tmp_path, **{field: value})


def test_llm_cache_cross_field_bounds_are_enforced(tmp_path) -> None:
    with pytest.raises(ValidationError, match="item"):
        Settings(
            working_dir=tmp_path,
            llm_local_cache_max_bytes=1024,
            llm_hot_cache_max_item_bytes=2048,
        )
    with pytest.raises(ValidationError, match="300"):
        Settings(working_dir=tmp_path, llm_local_cache_ttl_seconds=301)
    with pytest.raises(ValidationError, match="3600"):
        Settings(working_dir=tmp_path, llm_redis_cache_ttl_seconds=3601)
