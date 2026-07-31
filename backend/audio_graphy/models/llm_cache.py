"""Tenant-isolated, encrypted persistent cache for validated LLM results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
)
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase, _utcnow

_PORTABLE_BIGINT = BigInteger().with_variant(Integer, "sqlite")
_PORTABLE_MEDIUMBLOB = LargeBinary().with_variant(MEDIUMBLOB(), "mysql")


class LLMCacheEntry(TenantScopedBase):
    """One exact cache recipe and its lease/validated encrypted result."""

    __tablename__ = "llm_cache_entries"

    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_epoch: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    payload_encrypted: Mapped[bytes | None] = mapped_column(
        _PORTABLE_MEDIUMBLOB,
        nullable=True,
    )
    encryption_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    payload_size_bytes: Mapped[int] = mapped_column(
        _PORTABLE_BIGINT,
        nullable=False,
        default=0,
    )
    has_provenance: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    hit_count: Mapped[int] = mapped_column(
        _PORTABLE_BIGINT,
        nullable=False,
        default=0,
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Populated only for explicitly white-listed semantic helper tasks.
    semantic_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    semantic_guard_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    semantic_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    semantic_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready')",
            name="ck_llm_cache_entries_status",
        ),
        CheckConstraint(
            "payload_size_bytes >= 0 AND hit_count >= 0",
            name="ck_llm_cache_entries_counters",
        ),
        CheckConstraint(
            "semantic_dim IS NULL OR semantic_dim > 0",
            name="ck_llm_cache_entries_semantic_dim",
        ),
        Index(
            "ux_llm_cache_entries_identity",
            "tenant_id",
            "namespace",
            "recipe_sha256",
            unique=True,
        ),
        Index("ix_llm_cache_entries_expiry", "expires_at"),
        Index(
            "ix_llm_cache_entries_tenant_access",
            "tenant_id",
            "last_accessed_at",
        ),
        Index(
            "ix_llm_cache_entries_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_llm_cache_entries_semantic",
            "tenant_id",
            "namespace",
            "semantic_scope_hash",
            "semantic_guard_hash",
            "language",
            "last_accessed_at",
        ),
    )


class LLMCacheRef(TenantScopedBase):
    """Polymorphic source reference used for DSAR and retention erasure."""

    __tablename__ = "llm_cache_refs"

    cache_entry_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("llm_cache_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        Index(
            "ux_llm_cache_refs_entry_source",
            "cache_entry_id",
            "source_type",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_llm_cache_refs_tenant_source",
            "tenant_id",
            "source_type",
            "source_id",
        ),
    )


class LLMCacheSourceGuard(TenantScopedBase):
    """Serialization row and durable erasure tombstone for one source."""

    __tablename__ = "llm_cache_source_guards"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'erased')",
            name="ck_llm_cache_source_guards_state",
        ),
        Index(
            "ux_llm_cache_source_guards_identity",
            "tenant_id",
            "source_type",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_llm_cache_source_guards_erased",
            "state",
            "erased_at",
        ),
    )


class LLMCachePurge(TenantScopedBase):
    """Durable Redis/local invalidation that survives cache outages."""

    __tablename__ = "llm_cache_purges"

    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index(
            "ux_llm_cache_purges_identity",
            "tenant_id",
            "namespace",
            "recipe_sha256",
            unique=True,
        ),
        Index("ix_llm_cache_purges_created", "created_at"),
    )


__all__ = [
    "LLMCacheEntry",
    "LLMCachePurge",
    "LLMCacheRef",
    "LLMCacheSourceGuard",
]
