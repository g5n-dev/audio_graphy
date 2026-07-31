"""Portable metadata tests for the durable LLM cache tables."""

from __future__ import annotations

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from audio_graphy.models.llm_cache import (
    LLMCacheEntry,
    LLMCachePurge,
    LLMCacheRef,
    LLMCacheSourceGuard,
)
from audio_graphy.models.llm_call_log import LLMCallLog


def test_llm_cache_tables_are_registered_with_required_indexes() -> None:
    entry_table = LLMCacheEntry.__table__
    ref_table = LLMCacheRef.__table__
    guard_table = LLMCacheSourceGuard.__table__
    purge_table = LLMCachePurge.__table__

    assert entry_table.name == "llm_cache_entries"
    assert ref_table.name == "llm_cache_refs"
    assert guard_table.name == "llm_cache_source_guards"
    assert purge_table.name == "llm_cache_purges"
    assert {
        "ux_llm_cache_entries_identity",
        "ix_llm_cache_entries_expiry",
        "ix_llm_cache_entries_tenant_access",
        "ix_llm_cache_entries_lease",
        "ix_llm_cache_entries_semantic",
    } <= {index.name for index in entry_table.indexes}
    assert {
        "ux_llm_cache_refs_entry_source",
        "ix_llm_cache_refs_tenant_source",
    } <= {index.name for index in ref_table.indexes}
    assert {
        "ux_llm_cache_source_guards_identity",
        "ix_llm_cache_source_guards_erased",
    } <= {index.name for index in guard_table.indexes}
    assert {
        "ux_llm_cache_purges_identity",
        "ix_llm_cache_purges_created",
    } <= {index.name for index in purge_table.indexes}


def test_mysql_payload_column_uses_mediumblob() -> None:
    table = LLMCacheEntry.__table__
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    identity_index = next(
        index for index in table.indexes if index.name == "ux_llm_cache_entries_identity"
    )
    index_ddl = str(CreateIndex(identity_index).compile(dialect=mysql.dialect()))

    assert "MEDIUMBLOB" in ddl
    assert "CREATE UNIQUE INDEX" in index_ddl


def test_llm_call_log_exposes_compatible_cache_observation_fields() -> None:
    table = LLMCallLog.__table__

    assert table.c.purpose.server_default is not None
    assert table.c.cache_source.server_default is not None
    assert table.c.provider_called.server_default is not None
    assert table.c.event_kind.server_default is not None
    assert table.c.outcome.server_default is not None
    assert table.c.attempt.nullable
    assert table.c.error_type.nullable
