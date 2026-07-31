"""Back-compatible import path for the LLM gateway.

The implementation moved to ``audio_graphy.llm.gateway``. It never depended on
anything in ``services/`` — only on ``adapters`` — but living here forced five
``core/`` modules to import from ``services/`` to reach the request contract,
inverting the layering.

New code should import from ``audio_graphy.llm.gateway``. This module re-exports
the same objects (identity is preserved, so ``isinstance`` checks and monkey
patching behave as before) and is kept so existing importers keep working.
"""

from __future__ import annotations

from audio_graphy.llm.gateway import (
    CachedLLMValue,
    CachePolicy,
    LLMCache,
    LLMCacheIdentity,
    LLMGateway,
    LLMObservation,
    LLMObserver,
    LLMObserverCallback,
    LLMPriceSnapshot,
    LLMProvenance,
    LLMRequest,
    LLMUsageContext,
    RecipeMigrationMode,
    RecipeVersion,
    canonical_sha256,
    execute_llm,
    llm_request_memo_scope,
    lookup_llm_cache,
    store_validated_llm_cache,
)

__all__ = [
    "CachePolicy",
    "CachedLLMValue",
    "LLMCache",
    "LLMCacheIdentity",
    "LLMGateway",
    "LLMObservation",
    "LLMObserver",
    "LLMObserverCallback",
    "LLMPriceSnapshot",
    "LLMProvenance",
    "LLMRequest",
    "LLMUsageContext",
    "RecipeMigrationMode",
    "RecipeVersion",
    "canonical_sha256",
    "execute_llm",
    "llm_request_memo_scope",
    "lookup_llm_cache",
    "store_validated_llm_cache",
]
