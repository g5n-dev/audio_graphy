"""Durability of the tenant GraphML store: cross-process writes and corruption.

The graph is one file per tenant, rewritten in full on every save. Several
processes write it — the API replicas, the pipeline worker and the retention
scheduler all share the working_dir volume — so the publish must be atomic
against them, and an unreadable file must never be mistaken for an empty graph.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from pathlib import Path

import networkx as nx
import pytest

from audio_graphy.core.types import GraphNode
from audio_graphy.storage.graph_networkx import (
    GRAPHML_FILENAME,
    GraphStoreCorruptError,
    NetworkXGraphStore,
)

TENANT = "durability"


def _node(entity_id: str) -> GraphNode:
    return GraphNode(
        entity_id=entity_id,
        name=entity_id,
        type="实体",
        description="",
        source_ids=[],
        recording_ids=[],
        degree=0,
    )


def _write_many(working_dir: str, prefix: str, count: int) -> None:
    """Publish `count` saves from a separate process."""

    async def run() -> None:
        store = NetworkXGraphStore(Path(working_dir), tenant_id=TENANT)
        await store.load()
        for index in range(count):
            await store.upsert_node(_node(f"{prefix}-{index}"))
            await store.save()

    asyncio.run(run())


@pytest.mark.integration
def test_concurrent_processes_never_publish_a_torn_file(tmp_path: Path) -> None:
    """Two processes hammering the same tenant must leave a parseable file.

    Atomicity comes from the tmp-file + os.replace publish, so this is a
    regression guard on that: swap it for a direct write to the target and this
    test starts finding truncated GraphML.

    Last-write-wins on *content* is not fixed by that, and is deliberately not
    asserted here — the store logs when it overwrites a diverged version, which
    the supersede test below covers.
    """
    context = mp.get_context("spawn")
    workers = [
        context.Process(target=_write_many, args=(str(tmp_path), prefix, 12))
        for prefix in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    assert all(w.exitcode == 0 for w in workers), [w.exitcode for w in workers]

    published = tmp_path / TENANT / GRAPHML_FILENAME
    assert published.exists()
    # Parses cleanly: the publish was atomic even under contention.
    graph = nx.read_graphml(published)
    assert graph.number_of_nodes() > 0

    store = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
    asyncio.run(store.load())
    assert store.graph.number_of_nodes() > 0


@pytest.mark.unit
def test_corrupt_graphml_raises_instead_of_yielding_an_empty_graph(
    tmp_path: Path,
) -> None:
    """A parse failure must not present as "this tenant has no graph".

    Substituting an empty graph is what made corruption permanent: the caller
    carried on, and the next save wrote those zero nodes over the only copy.
    """
    path = tmp_path / TENANT / GRAPHML_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("<graphml> this is not valid", encoding="utf-8")

    store = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
    with pytest.raises(GraphStoreCorruptError):
        asyncio.run(store.load())


@pytest.mark.unit
def test_corrupt_graphml_bytes_stay_recoverable(tmp_path: Path) -> None:
    """The unreadable file is moved aside, not deleted."""
    path = tmp_path / TENANT / GRAPHML_FILENAME
    path.parent.mkdir(parents=True)
    original = "<graphml> truncated mid-write"
    path.write_text(original, encoding="utf-8")

    store = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
    with pytest.raises(GraphStoreCorruptError) as excinfo:
        asyncio.run(store.load())

    quarantined = excinfo.value.quarantined
    assert quarantined is not None
    assert quarantined.read_text(encoding="utf-8") == original
    # The corrupt file no longer occupies the canonical path, so a later write
    # starts from a clean slate rather than tripping over it again.
    assert not path.exists()


@pytest.mark.unit
def test_missing_graphml_is_still_an_empty_graph(tmp_path: Path) -> None:
    """Absent is not corrupt — a fresh tenant must load without error."""
    store = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
    asyncio.run(store.load())
    assert store.graph.number_of_nodes() == 0


@pytest.mark.unit
def test_save_reports_overwriting_another_writer(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Divergent writers are logged rather than silently reconciled."""

    async def scenario() -> None:
        first = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
        await first.load()
        await first.upsert_node(_node("first"))
        await first.save()

        # A second holder of the same tenant, unaware of `first`.
        second = NetworkXGraphStore(tmp_path, tenant_id=TENANT)
        await second.load()

        await first.upsert_node(_node("first-again"))
        await first.save()

        await second.upsert_node(_node("second"))
        await second.save()

    with caplog.at_level("ERROR"):
        asyncio.run(scenario())

    assert any("changed under us" in record.message for record in caplog.records)
