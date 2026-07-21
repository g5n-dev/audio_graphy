"""EntityMerger — 3-layer entity normalisation (DB alias → rapidfuzz → new).

M6 WS-3 implementation. Replaces the M5 ``_DEFAULT_ALIASES`` hard-coded dict
in ``core/extractor.py`` with a tenant-scoped, hot-editable, fuzzy-tolerant
normaliser.

Flow (per docs/m6-architecture.md §1.5.4)::

    raw entity: "长安 CS75 Plus"
        │
        ▼
    Layer 1: NFKC + lowercase + strip → "长安 cs75 plus"
        │
        ▼
    Layer 2: DB entity_aliases WHERE tenant_id=X AND alias_text="长安 cs75 plus"
        │  hit → canonical_text (manual alias wins)
        │  miss ↓
    Layer 3: rapidfuzz fuzz.WRatio vs existing canonicals (in-memory cache)
        │  score >= threshold (0.85) → merge + record new fuzzy alias row
        │  miss ↓
    Layer 4: leave as-is → upstream persists as a new canonical

Performance notes:
    - ``rapidfuzz.process.extractOne`` is used (C++ backed, ~10μs / comparison).
    - ``canonicals`` list is small (≤ 10^3 per run in practice), so we hit
      O(N × C) but each comparison is microseconds.
    - For batches ≥ 50, the in-memory cache dominates; the DB is hit once
      at construction (``_load_aliases_for_tenant``).

Tenant isolation:
    - ``EntityAlias`` rows are tenant-scoped via ``TenantScopedBase``.
    - ``EntityMerger`` only ever queries ``WHERE tenant_id = self._tenant_id``.

Audit:
    - New fuzzy aliases are written with ``source="fuzzy_match"`` and the
      WRatio score as ``confidence``. Manual aliases (``source="manual"``)
    are never overwritten.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.entity_alias import EntityAlias

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _AliasCacheEntry:
    """One alias lookup entry cached in memory."""

    canonical_text: str
    entity_type: str | None
    source: str
    confidence: float


@dataclass(frozen=True, slots=True)
class _CanonicalEntry:
    """One canonical-form entry tracked for fuzzy matching.

    Attributes:
        display: Original-case display string (e.g. ``"CS75 Plus"``).
        entity_type: Domain type, or ``None`` if the canonical is type-agnostic
            (typed aliases still match untyped queries and vice-versa).
    """

    display: str
    entity_type: str | None


def _norm(s: str) -> str:
    """NFKC + lowercase + strip. Idempotent.

    NFKC folds fullwidth/halfwidth variants (``ＣＳ７５`` → ``CS75``).
    Lowercase + strip handle case/whitespace noise.
    """
    return unicodedata.normalize("NFKC", s).strip().lower()


def _types_compatible(alias_type: str | None, ent_type: str | None) -> bool:
    """``True`` if an alias with ``alias_type`` may apply to an entity of ``ent_type``.

    Rule: ``None`` is a wildcard (matches any). Otherwise the two must be equal.
    """
    if alias_type is None or ent_type is None:
        return True
    return alias_type == ent_type


class EntityMerger:
    """3-layer entity merger: DB alias → rapidfuzz → new canonical.

    Usage::

        merger = EntityMerger(session, tenant_id="chang_an",
                              fuzzy_threshold=settings.entity_fuzzy_threshold)
        entities = await merger.merge(extracted_entities)

    Args:
        session_factory: async session maker (used for both reading aliases
            and writing new fuzzy_match alias rows).
        tenant_id: Tenant scope (M6: single-tenant per merger instance).
        fuzzy_threshold: rapidfuzz WRatio threshold ∈ [0.0, 1.0]. Default 0.85.
            When set to ``1.0``, fuzzy matching is effectively disabled
            (only exact DB-alias matches apply).
        persist_fuzzy_aliases: When ``True`` (default), each fuzzy hit is
            recorded as a new ``entity_aliases`` row with
            ``source="fuzzy_match"`` and ``confidence=<score>``. Disable
            in read-only / eval contexts.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: str,
        *,
        fuzzy_threshold: float = 0.85,
        persist_fuzzy_aliases: bool = True,
    ) -> None:
        if not 0.0 <= fuzzy_threshold <= 1.0:
            raise ValueError(
                f"fuzzy_threshold must be in [0.0, 1.0], got {fuzzy_threshold}"
            )
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._threshold = fuzzy_threshold
        self._persist = persist_fuzzy_aliases

        # Lazy-loaded caches.
        self._alias_cache: dict[str, _AliasCacheEntry] | None = None
        self._canonical_index: dict[str, list[_CanonicalEntry]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def merge(
        self,
        entities: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Merge a list of ``(name, entity_type)`` tuples.

        Args:
            entities: Raw extracted ``(name, type)`` pairs.

        Returns:
            New list with normalised ``(canonical_name, type)`` pairs,
            preserving the input order. The returned list has the same
            length as the input.
        """
        if not entities:
            return []

        await self._ensure_cache_loaded()

        out: list[tuple[str, str]] = []
        # Track new fuzzy hits so we can batch-insert at the end.
        new_fuzzy: list[tuple[str, str, str | None, float]] = []

        for raw_name, ent_type in entities:
            canonical, _score = await self._resolve_one(raw_name, ent_type, new_fuzzy)
            out.append((canonical, ent_type))
            # Register canonical in the in-memory cache so subsequent
            # near-dups in the same batch match against it.
            self._register_canonical(canonical, ent_type)

        if new_fuzzy and self._persist:
            await self._persist_fuzzy_aliases(new_fuzzy)

        return out

    async def merge_records(
        self,
        records: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        """Merge ``(name, type, description)`` triples preserving description.

        Convenience wrapper for callers that carry description alongside
        (name, type). Only ``name`` is rewritten; ``type`` / ``description``
        pass through unchanged.
        """
        pairs = [(r[0], r[1]) for r in records]
        merged_pairs = await self.merge(pairs)
        return [(m[0], r[1], r[2]) for m, r in zip(merged_pairs, records, strict=True)]

    # ------------------------------------------------------------------
    # Per-entity resolution
    # ------------------------------------------------------------------
    async def _resolve_one(
        self,
        raw_name: str,
        ent_type: str,
        new_fuzzy: list[tuple[str, str, str | None, float]],
    ) -> tuple[str, float]:
        """Resolve one entity name → (canonical_name, score).

        Score is 1.0 for exact match (DB alias or normalised identity),
        WRatio (0..1) for fuzzy match, or 0.0 for new canonical.
        """
        norm_name = _norm(raw_name)
        if not norm_name:
            return raw_name, 1.0

        assert self._alias_cache is not None
        assert self._canonical_index is not None

        # Layer 1: exact DB alias hit (normalised alias_text lookup).
        cached = self._alias_cache.get(norm_name)
        # Type filter: alias matches only if its type is null or equal.
        if cached is not None and _types_compatible(cached.entity_type, ent_type):
            return cached.canonical_text, cached.confidence

        # Layer 2: identity (normalised name matches an existing canonical of
        # a compatible type).
        existing_entries = self._canonical_index.get(norm_name, [])
        compatible = [e for e in existing_entries if _types_compatible(e.entity_type, ent_type)]
        if compatible:
            # Already a known canonical — pick the first compatible one.
            return compatible[0].display, 1.0

        # Layer 3: rapidfuzz WRatio against existing canonicals.
        # Build a candidate list of (norm_canonical, type) pairs that are
        # type-compatible with the query.
        candidates: list[str] = []
        candidate_display: list[str] = []
        for norm_canonical, entries in self._canonical_index.items():
            for entry in entries:
                if _types_compatible(entry.entity_type, ent_type):
                    candidates.append(norm_canonical)
                    candidate_display.append(entry.display)
        if candidates:
            best = process.extractOne(
                norm_name,
                candidates,
                scorer=fuzz.WRatio,
                score_cutoff=int(self._threshold * 100),
            )
            if best is not None:
                _best_key, score_int, idx = best
                score = score_int / 100.0
                canonical_display = candidate_display[idx]
                # Queue fuzzy alias for persistence.
                new_fuzzy.append((norm_name, canonical_display, ent_type, score))
                return canonical_display, score

        # Layer 4: new canonical — keep original name.
        return raw_name, 0.0

    def _register_canonical(self, canonical: str, ent_type: str) -> None:
        """Track ``canonical`` in the in-memory cache for subsequent fuzzy hits."""
        assert self._canonical_index is not None
        norm_canonical = _norm(canonical)
        if not norm_canonical:
            return
        entries = self._canonical_index.setdefault(norm_canonical, [])
        # Avoid duplicates of (display, type).
        for e in entries:
            if e.display == canonical and _types_compatible(e.entity_type, ent_type):
                return
        entries.append(_CanonicalEntry(display=canonical, entity_type=ent_type))
        # Also seed alias_cache so a later identical raw name hits Layer 1.
        assert self._alias_cache is not None
        self._alias_cache.setdefault(
            norm_canonical,
            _AliasCacheEntry(
                canonical_text=canonical,
                entity_type=ent_type,
                source="identity",
                confidence=1.0,
            ),
        )

    # ------------------------------------------------------------------
    # Cache + persistence
    # ------------------------------------------------------------------
    async def _ensure_cache_loaded(self) -> None:
        """Lazily load alias + canonical caches from the DB (one-shot)."""
        if self._alias_cache is not None:
            return
        alias_cache: dict[str, _AliasCacheEntry] = {}
        canonical_index: dict[str, list[_CanonicalEntry]] = {}
        try:
            async with self._session_factory() as session:
                stmt = select(EntityAlias).where(
                    EntityAlias.tenant_id == self._tenant_id
                )
                result = await session.execute(stmt)
                for row in result.scalars().all():
                    norm_alias = _norm(row.alias_text)
                    if not norm_alias:
                        continue
                    alias_cache[norm_alias] = _AliasCacheEntry(
                        canonical_text=row.canonical_text,
                        entity_type=row.entity_type,
                        source=row.source,
                        confidence=float(row.confidence),
                    )
                    norm_canonical = _norm(row.canonical_text)
                    if norm_canonical:
                        entries = canonical_index.setdefault(norm_canonical, [])
                        # Avoid duplicates of (display, type).
                        already = any(
                            e.display == row.canonical_text
                            and _types_compatible(e.entity_type, row.entity_type)
                            for e in entries
                        )
                        if not already:
                            entries.append(
                                _CanonicalEntry(
                                    display=row.canonical_text,
                                    entity_type=row.entity_type,
                                )
                            )
        except Exception as exc:
            logger.warning(
                "EntityMerger: alias cache load failed for tenant=%s: %s — "
                "operating on empty cache (fuzzy-only mode)",
                self._tenant_id,
                exc,
            )

        self._alias_cache = alias_cache
        self._canonical_index = canonical_index

    async def _persist_fuzzy_aliases(
        self,
        new_fuzzy: list[tuple[str, str, str | None, float]],
    ) -> None:
        """Insert the queued fuzzy aliases as ``source="fuzzy_match"`` rows.

        Existing rows (manual or fuzzy) are skipped silently — manual
        aliases are never overwritten, and fuzzy aliases are idempotent.
        """
        if not new_fuzzy:
            return
        try:
            async with self._session_factory() as session:
                for alias_text, canonical, ent_type, score in new_fuzzy:
                    # Re-check uniqueness against DB (avoids IntegrityError
                    # if another process inserted in the meantime).
                    existing = await session.execute(
                        select(EntityAlias).where(
                            EntityAlias.tenant_id == self._tenant_id,
                            EntityAlias.alias_text == alias_text,
                        )
                    )
                    if existing.scalar_one_or_none() is not None:
                        continue
                    session.add(
                        EntityAlias(
                            tenant_id=self._tenant_id,
                            canonical_text=canonical,
                            alias_text=alias_text,
                            entity_type=ent_type,
                            source="fuzzy_match",
                            confidence=float(score),
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "EntityMerger: fuzzy alias persistence failed for tenant=%s: %s",
                self._tenant_id,
                exc,
            )


__all__ = ["EntityMerger"]
