"""Configuration gates for bounded vector and graph caches."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_graphy.config import Settings


@pytest.mark.unit
def test_performance_resource_defaults_are_bounded(tmp_path) -> None:
    settings = Settings(working_dir=tmp_path)

    assert settings.graph_store_cache_max_entries >= 1
    assert settings.vector_index_load_batch_rows >= 1
    assert settings.vector_index_load_max_rows == 100_000
    assert settings.vector_index_load_max_memory_bytes <= 512 * 1024 * 1024


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_store_cache_max_entries", 0),
        ("vector_index_cache_max_entries", 0),
        ("vector_index_cache_max_bytes", 0),
        ("vector_index_load_batch_rows", 0),
        ("vector_index_load_max_rows", 0),
        ("vector_index_load_max_source_bytes", 0),
        ("vector_index_load_max_memory_bytes", 0),
    ],
)
def test_performance_resource_limits_must_be_positive(
    tmp_path,
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(working_dir=tmp_path, **{field: value})
