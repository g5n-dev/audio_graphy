"""Unit tests for EntityMerger — 3-layer Chinese entity normalisation (M6 WS-3).

Cases cover all 8 acceptance scenarios from docs/m6-architecture.md §7.3:
    1. Exact DB alias match (Layer 1, manual source).
    2. Fuzzy match above threshold (Layer 3, ``CS75 Plus`` ↔ ``CS75PLUS``).
    3. Fuzzy match below threshold (no merge; ``CS75`` vs ``CS35``).
    4. Manual alias precedence over fuzzy (Layer 1 wins before Layer 3 runs).
    5. Tenant isolation (other tenant's aliases are invisible).
    6. Entity type filter (alias with explicit type filters by type).
    7. NFKC normalization (全角 → 半角).
    8. Large batch (20 entities; performance sanity + alias persistence).

The fuzzy_threshold=0.85 default matches docs/m6-prd.md §6.1 Q3 (locked).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401 — register all models on Base.metadata
from audio_graphy.core.entity_merger import EntityMerger
from audio_graphy.models.base import Base
from audio_graphy.models.entity_alias import EntityAlias


@pytest_asyncio.fixture
async def em_engine() -> AsyncIterator[Any]:
    """In-memory SQLite async engine for entity_aliases tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def em_factory(em_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(em_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_alias(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    canonical: str,
    alias: str,
    entity_type: str | None = None,
    source: str = "manual",
    confidence: float = 1.0,
) -> None:
    """Insert one entity_alias row."""
    async with factory() as session:
        session.add(
            EntityAlias(
                tenant_id=tenant_id,
                canonical_text=canonical,
                alias_text=alias,
                entity_type=entity_type,
                source=source,
                confidence=confidence,
            )
        )
        await session.commit()


# --------------------------------------------------------------------
# Case 1 — exact DB alias match
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_db_alias_match(em_factory: async_sessionmaker[AsyncSession]) -> None:
    """A manual alias in DB replaces the raw alias_text with canonical."""
    await _seed_alias(
        em_factory,
        tenant_id="t1",
        canonical="CS75 Plus",
        alias="cs75plus",
        entity_type="车型",
    )
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out = await merger.merge([("cs75plus", "车型")])
    assert out == [("CS75 Plus", "车型")]


# --------------------------------------------------------------------
# Case 2 — fuzzy match above threshold
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fuzzy_match_above_threshold(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``CS75PLUS`` fuzzy-matches ``CS75 Plus`` ≥ 0.85 (no DB alias needed).

    The first entity seeds the canonical; the second near-dup matches it.
    """
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out = await merger.merge([
        ("CS75 Plus", "车型"),
        ("CS75PLUS", "车型"),
    ])
    # First entity becomes canonical; second merges into it.
    assert out[0][0] == "CS75 Plus"
    assert out[1][0] == "CS75 Plus"
    assert out[0][1] == out[1][1] == "车型"


# --------------------------------------------------------------------
# Case 3 — fuzzy match below threshold (no merge)
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fuzzy_match_below_threshold(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``CS75`` vs ``CS35`` are too far apart; left as separate canonicals."""
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out = await merger.merge([
        ("CS75", "车型"),
        ("CS35", "车型"),
    ])
    assert out[0][0] == "CS75"
    assert out[1][0] == "CS35"


# --------------------------------------------------------------------
# Case 4 — manual alias precedence over fuzzy
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_alias_precedence(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A manual alias wins over a fuzzy match even when both could apply.

    Setup:
        DB alias: cs75plus -> "Manual Canonical" (source=manual)
        Input sequence: ["CS75 Plus", "cs75plus"]
    Expected: cs75plus resolves to "Manual Canonical" (Layer 1),
              NOT to "CS75 Plus" (Layer 3 fuzzy).
    """
    await _seed_alias(
        em_factory,
        tenant_id="t1",
        canonical="Manual Canonical",
        alias="cs75plus",
        entity_type="车型",
        source="manual",
    )
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out = await merger.merge([
        ("CS75 Plus", "车型"),
        ("cs75plus", "车型"),
    ])
    assert out[0][0] == "CS75 Plus"
    # Layer 1 hit: cs75plus -> "Manual Canonical"
    assert out[1][0] == "Manual Canonical"


# --------------------------------------------------------------------
# Case 5 — tenant isolation
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_isolation(em_factory: async_sessionmaker[AsyncSession]) -> None:
    """Aliases seeded for tenant-A are invisible to tenant-B."""
    await _seed_alias(
        em_factory,
        tenant_id="tenant_a",
        canonical="CS75 Plus",
        alias="cs75plus",
        entity_type="车型",
    )
    merger = EntityMerger(em_factory, "tenant_b", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    # Tenant B has no alias → no Layer 1 hit, but Layer 3 fuzzy still works
    # in-batch (cs75plus still fuzzy-matches "CS75 Plus" canonical).
    out = await merger.merge([("cs75plus", "车型")])
    # Single entity: no fuzzy candidate → stays as-is.
    assert out == [("cs75plus", "车型")]

    # Confirm no cross-tenant leak in DB.
    async with em_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(EntityAlias).where(EntityAlias.tenant_id == "tenant_b")
                )
            ).scalars().all()
        )
        assert rows == []


# --------------------------------------------------------------------
# Case 6 — entity type filter
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_type_filter(em_factory: async_sessionmaker[AsyncSession]) -> None:
    """Alias with explicit entity_type only matches same-typed entities."""
    await _seed_alias(
        em_factory,
        tenant_id="t1",
        canonical="CS75 Plus",
        alias="cs75",
        entity_type="车型",  # only matches "车型"
    )
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    # Same type → alias applies.
    out_same = await merger.merge([("cs75", "车型")])
    assert out_same == [("CS75 Plus", "车型")]

    # Different type → alias skipped; entity stays as-is.
    merger2 = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out_diff = await merger2.merge([("cs75", "竞品")])
    assert out_diff == [("cs75", "竞品")]


# --------------------------------------------------------------------
# Case 7 — NFKC normalization (全角 → 半角)
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nfkc_normalization(em_factory: async_sessionmaker[AsyncSession]) -> None:
    """Fullwidth chars ｃｓ７５ collapse to ASCII cs75 before fuzzy compare."""
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    out = await merger.merge([
        ("CS75 Plus", "车型"),
        ("ｃｓ７５ ｐｌｕｓ", "车型"),  # fullwidth variant
    ])
    # Fullwidth normalises to lowercase cs75 plus; WRatio >= 0.85 vs "cs75 plus".
    assert out[0][0] == "CS75 Plus"
    assert out[1][0] == "CS75 Plus"


# --------------------------------------------------------------------
# Case 8 — large batch + alias persistence
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_batch_and_persist(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """20-entity batch completes; fuzzy aliases persisted as source=fuzzy_match.

    Note: 哈弗H1 / 哈弗H10 / 哈弗H11 are all WRatio ≥ 0.85 (nearly identical
    strings), so the test uses strongly distinct competitor names instead.
    """
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=True)
    raw = (
        [("CS75 Plus", "车型")]
        + [(f"CS75 Plus {i}", "车型") for i in range(1, 10)]  # near-dups → merge
        + [(f"竞品-{x}", "竞品") for x in "ABCDEFGHIJ"]  # 10 distinct competitors
    )
    out = await merger.merge(raw)
    assert len(out) == 20
    # All "CS75 Plus *" should merge into "CS75 Plus".
    cs75_count = sum(1 for name, _ in out if name == "CS75 Plus")
    assert cs75_count == 10
    # Competitors stay distinct (names too different to fuzzy-merge).
    competitors = sorted({name for name, t in out if t == "竞品"})
    assert len(competitors) == 10

    # Verify fuzzy aliases persisted to DB.
    async with em_factory() as session:
        fuzzy_rows = list(
            (
                await session.execute(
                    select(EntityAlias).where(
                        EntityAlias.tenant_id == "t1",
                        EntityAlias.source == "fuzzy_match",
                    )
                )
            ).scalars().all()
        )
    assert len(fuzzy_rows) >= 1
    assert all(r.confidence >= 0.85 for r in fuzzy_rows)


# --------------------------------------------------------------------
# Bonus — fuzzy_threshold=1.0 disables fuzzy (strict mode)
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_threshold_1_disables_fuzzy(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """threshold=1.0 → fuzzy matching effectively disabled; only Layer 1 applies."""
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=1.0, persist_fuzzy_aliases=False)
    out = await merger.merge([
        ("CS75 Plus", "车型"),
        ("CS75PLUS", "车型"),
    ])
    # No fuzzy match → both left as distinct canonicals.
    assert out[0][0] == "CS75 Plus"
    assert out[1][0] == "CS75PLUS"


@pytest.mark.asyncio
async def test_invalid_threshold_raises(
    em_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Threshold outside [0.0, 1.0] raises ValueError."""
    with pytest.raises(ValueError, match="fuzzy_threshold"):
        EntityMerger(em_factory, "t1", fuzzy_threshold=1.5)
    with pytest.raises(ValueError, match="fuzzy_threshold"):
        EntityMerger(em_factory, "t1", fuzzy_threshold=-0.1)


@pytest.mark.asyncio
async def test_empty_input(em_factory: async_sessionmaker[AsyncSession]) -> None:
    """Empty list returns empty without touching DB."""
    merger = EntityMerger(em_factory, "t1", fuzzy_threshold=0.85, persist_fuzzy_aliases=False)
    assert await merger.merge([]) == []
