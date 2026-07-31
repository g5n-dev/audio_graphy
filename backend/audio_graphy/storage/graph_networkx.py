"""NetworkX knowledge graph store — MultiDiGraph CRUD + GraphML persistence.

Each tenant has one GraphML file: ``working_dir/{tenant_id}/graph_chunk_entity_relation.graphml``.

Uses ``nx.MultiDiGraph`` so that the same entity pair can have multiple
relation types (e.g. ``(客户)-[询问]→(CS75 Plus)`` and ``(客户)-[对比]→(CS75 Plus)``).
Edge key = relation string.

GraphML serialisation: list attributes (source_ids, recording_ids) are stored
as JSON strings because GraphML does not support native list types.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no advisory file locking
    fcntl = None  # type: ignore[assignment]

import networkx as nx

from audio_graphy.adapters.protocols import EdgeConfidence
from audio_graphy.core.types import (
    GraphEdge,
    GraphNode,
    _list_to_str,
    _str_to_list,
    upgrade_confidence,
)

logger = logging.getLogger(__name__)

GRAPHML_FILENAME = "graph_chunk_entity_relation.graphml"


class GraphStoreCorruptError(RuntimeError):
    """The tenant's GraphML file could not be parsed.

    Raised instead of substituting an empty graph, which would be written back
    over the only copy on the next save.
    """

    def __init__(self, path: Path, quarantined: Path | None) -> None:
        self.path = path
        self.quarantined = quarantined
        location = f" (moved to {quarantined})" if quarantined else ""
        super().__init__(f"Corrupt graph store at {path}{location}")


def cast_edge_confidence(value: str) -> EdgeConfidence:
    """Safely cast a string to EdgeConfidence, defaulting to AMBIGUOUS.

    M9 L7 introduces ``DEPRECATED`` as a valid EdgeConfidence value; it is
    preserved here (rather than collapsed to AMBIGUOUS) so the L7
    deprecation + retrieval exclusion path round-trips through storage.
    """
    if value in ("EXTRACTED", "INFERRED", "AMBIGUOUS", "DEPRECATED"):
        return cast(EdgeConfidence, value)
    return "AMBIGUOUS"


class NetworkXGraphStore:
    """NetworkX MultiDiGraph knowledge graph with GraphML file persistence.

    Args:
        working_dir: Root working_dir path.
        tenant_id: Tenant ID (determines sub-directory + GraphML file path).
    """

    def __init__(self, working_dir: Path, *, tenant_id: str = "default") -> None:
        self._working_dir = Path(working_dir)
        self._tenant_id = tenant_id
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._loaded = False
        self._graph_revision = 0
        self._path_projection_revision = -1
        self._path_projection: nx.Graph | None = None
        self._path_projection_lock = asyncio.Lock()
        self._path_projection_builds = 0
        # Retention runs in an APScheduler thread with its own event loop, while
        # request/pipeline saves run on the application loop. A threading lock
        # therefore protects the publish step across both execution contexts.
        self._save_lock = threading.Lock()
        # Identity of the bytes this instance last read or wrote, so a save can
        # tell whether another process published in the meantime.
        self._file_identity: tuple[int, int] | None = None

    @property
    def lock_path(self) -> Path:
        """Advisory lock file guarding cross-process publishes."""
        return self.graphml_path.with_name(f".{GRAPHML_FILENAME}.lock")

    def _current_file_identity(self) -> tuple[int, int] | None:
        """(mtime_ns, size) of the published GraphML, or None when absent."""
        try:
            stat = self.graphml_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    @contextlib.contextmanager
    def _cross_process_lock(self) -> Iterator[None]:
        """Serialise publishes against other processes sharing the working_dir.

        Publishing is already atomic — the tmp-file + ``os.replace`` below means
        a reader never sees a half-written file, with or without this lock. What
        the lock adds is that the check-then-write in ``_sync_save`` runs as a
        unit, so the supersede detection cannot itself race and miss an
        overwrite it was meant to report.

        ``_save_lock`` only covers threads inside one interpreter, but the
        pipeline worker, the retention scheduler and every API replica write the
        same file, and the working_dir is a shared volume in the Compose
        topology — so the lock has to live on the filesystem.

        Degrades to a no-op where fcntl is unavailable (Windows); the in-process
        lock still applies there.
        """
        if fcntl is None:
            yield
            return
        self.graphml_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @property
    def graphml_path(self) -> Path:
        """Full path to the tenant's GraphML file."""
        return self._working_dir / self._tenant_id / GRAPHML_FILENAME

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Direct access to the underlying NetworkX graph (for advanced queries)."""
        return self._graph

    @property
    def path_projection_builds(self) -> int:
        """Number of undirected projections built since store creation."""

        return self._path_projection_builds

    def invalidate_path_projection(self) -> None:
        """Mark the cached shortest-path projection stale after direct mutation."""

        self._graph_revision += 1
        self._path_projection = None

    # ------------------------------------------------------------------
    # Node CRUD
    # ------------------------------------------------------------------

    async def upsert_node(self, node: GraphNode) -> None:
        """Insert or update a node in the graph.

        If a node with the same entity_id already exists, its attributes are
        merged: source_ids and recording_ids are extended, degree is updated.

        Args:
            node: GraphNode to upsert.
        """
        await self._ensure_loaded()
        if self._graph.has_node(node.entity_id):
            existing = self._graph.nodes[node.entity_id]
            # Merge source_ids (union, preserve order)
            existing_ids = set(_str_to_list(existing.get("source_ids", "[]")))
            new_ids = existing_ids | set(node.source_ids)
            existing["source_ids"] = _list_to_str(list(new_ids))

            # Merge recording_ids (union)
            existing_rec = set(_str_to_list(existing.get("recording_ids", "[]")))
            new_rec = existing_rec | set(node.recording_ids)
            existing["recording_ids"] = _list_to_str(list(new_rec))

            # Update other attributes (take the new values)
            existing["name"] = node.name
            existing["type"] = node.type
            existing["description"] = node.description
            existing["degree"] = node.degree
        else:
            self._graph.add_node(
                node.entity_id,
                name=node.name,
                type=node.type,
                description=node.description,
                source_ids=_list_to_str(node.source_ids),
                recording_ids=_list_to_str(node.recording_ids),
                degree=node.degree,
            )
        self.invalidate_path_projection()

    async def get_node(self, entity_id: str) -> GraphNode | None:
        """Retrieve a node by entity_id.

        Args:
            entity_id: The normalised entity name (node key).

        Returns:
            GraphNode if found, None otherwise.
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return None
        attrs = self._graph.nodes[entity_id]
        return self._attrs_to_node(entity_id, attrs)

    async def get_all_nodes(self) -> list[GraphNode]:
        """Return all nodes in the graph.

        Returns:
            List of all GraphNode objects.
        """
        await self._ensure_loaded()
        return [
            self._attrs_to_node(node_id, attrs) for node_id, attrs in self._graph.nodes(data=True)
        ]

    # ------------------------------------------------------------------
    # Edge CRUD
    # ------------------------------------------------------------------

    async def upsert_edge(self, edge: GraphEdge) -> None:
        """Insert or update an edge in the graph.

        If an edge with the same (source, target, relation) already exists,
        its weight is accumulated and source_ids are extended. Confidence is
        upgraded per the EXTRACTED > INFERRED > AMBIGUOUS rule.

        Args:
            edge: GraphEdge to upsert.
        """
        await self._ensure_loaded()
        # Ensure nodes exist
        for node_id in (edge.source, edge.target):
            if not self._graph.has_node(node_id):
                self._graph.add_node(
                    node_id,
                    name=node_id,
                    type="未知",
                    description="",
                    source_ids="[]",
                    recording_ids="[]",
                    degree=0,
                )

        if self._graph.has_edge(edge.source, edge.target, key=edge.relation):
            existing = self._graph[edge.source][edge.target][edge.relation]
            existing["weight"] = float(existing.get("weight", 0.0)) + edge.weight
            # Merge source_ids
            existing_ids = set(_str_to_list(existing.get("source_ids", "[]")))
            existing_ids.update(edge.source_ids)
            existing["source_ids"] = _list_to_str(list(existing_ids))
            # Upgrade confidence

            old_conf = existing.get("confidence", "AMBIGUOUS")
            new_conf = upgrade_confidence(old_conf, edge.confidence)
            existing["confidence"] = new_conf
            # Recompute confidence_score
            existing["confidence_score"] = self._compute_score(new_conf, existing["weight"])
        else:
            self._graph.add_edge(
                edge.source,
                edge.target,
                key=edge.relation,
                relation=edge.relation,
                weight=edge.weight,
                confidence=edge.confidence,
                confidence_score=edge.confidence_score,
                source_ids=_list_to_str(edge.source_ids),
            )
        self.invalidate_path_projection()

    async def get_edges(self, entity_id: str) -> list[GraphEdge]:
        """Get all edges connected to an entity (both as source and target).

        Args:
            entity_id: The entity to look up.

        Returns:
            List of GraphEdge objects.
        """
        await self._ensure_loaded()
        edges: list[GraphEdge] = []
        # Outgoing edges
        for source, target, key, attrs in self._graph.out_edges(entity_id, data=True, keys=True):
            edges.append(self._attrs_to_edge(source, target, key, attrs))
        # Incoming edges
        for source, target, key, attrs in self._graph.in_edges(entity_id, data=True, keys=True):
            edge = self._attrs_to_edge(source, target, key, attrs)
            if edge not in edges:
                edges.append(edge)
        return edges

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------

    async def get_neighbors(self, entity_id: str, *, max_hops: int = 1) -> list[GraphNode]:
        """Get neighbor nodes within ``max_hops`` of the given entity.

        Args:
            entity_id: Starting entity.
            max_hops: Maximum hop distance (default 1 = direct neighbors).

        Returns:
            List of neighbor GraphNode objects (excluding the start node).
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return []

        if max_hops <= 0:
            return []

        # Use BFS to find neighbors within max_hops
        visited: set[str] = {entity_id}
        frontier: set[str] = {entity_id}

        for _hop in range(max_hops):
            next_frontier: set[str] = set()
            for node in frontier:
                # Outgoing neighbors
                for target in self._graph.successors(node):
                    if target not in visited:
                        next_frontier.add(target)
                # Incoming neighbors
                for source in self._graph.predecessors(node):
                    if source not in visited:
                        next_frontier.add(source)
            visited.update(next_frontier)
            frontier = next_frontier

        visited.discard(entity_id)
        return [
            self._attrs_to_node(nid, self._graph.nodes[nid])
            for nid in visited
            if self._graph.has_node(nid)
        ]

    async def get_relation_counts(self, entity_id: str) -> dict[str, int]:
        """Count edges by relation type for an entity.

        Used by the graph retrieval channel to rank neighbors by
        graph-structure signal (not pure vector similarity).

        Args:
            entity_id: The entity to analyse.

        Returns:
            Dict mapping relation type → count.
        """
        await self._ensure_loaded()
        counts: dict[str, int] = {}
        for _, _, key in self._graph.out_edges(entity_id, keys=True):
            counts[key] = counts.get(key, 0) + 1
        for _, _, key in self._graph.in_edges(entity_id, keys=True):
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def get_node_degree(self, entity_id: str) -> int:
        """Get the degree (total in + out edges) of a node.

        Args:
            entity_id: The entity to look up.

        Returns:
            Total degree, or 0 if node doesn't exist.
        """
        await self._ensure_loaded()
        if not self._graph.has_node(entity_id):
            return 0
        return int(self._graph.degree(entity_id))

    async def shortest_path(self, source: str, target: str) -> list[str]:
        """Return an undirected shortest path using a revisioned projection.

        Projection construction is moved off the event loop and reused by hot
        queries. Store mutation methods invalidate the cached projection.
        Call :meth:`invalidate_path_projection` after any deliberate direct
        mutation through :attr:`graph`.
        """

        await self._ensure_loaded()
        projection = await self._get_path_projection()
        path = await asyncio.to_thread(
            nx.shortest_path,
            projection,
            source=source,
            target=target,
        )
        return [str(node_id) for node_id in path]

    async def _get_path_projection(self) -> nx.Graph:
        if (
            self._path_projection is not None
            and self._path_projection_revision == self._graph_revision
        ):
            return self._path_projection

        async with self._path_projection_lock:
            if (
                self._path_projection is not None
                and self._path_projection_revision == self._graph_revision
            ):
                return self._path_projection

            while True:
                revision = self._graph_revision
                snapshot = self._graph.copy(as_view=False)
                projection = await asyncio.to_thread(nx.Graph, snapshot)
                if revision == self._graph_revision:
                    self._path_projection = projection
                    self._path_projection_revision = revision
                    self._path_projection_builds += 1
                    return projection

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save(self) -> None:
        """Atomically flush the in-memory graph to GraphML.

        Data is written and fsynced in a same-directory temporary file before
        ``os.replace`` publishes it.  A failed write or replace therefore
        leaves the last good GraphML intact.

        Raises:
            StorageError: If write fails.
        """
        from audio_graphy.core.types import StorageError

        self.graphml_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._sync_save)
        except Exception as exc:
            raise StorageError(f"Failed to save GraphML to {self.graphml_path}: {exc}") from exc
        logger.debug(
            "Saved graph (%d nodes, %d edges) to %s",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
            self.graphml_path,
        )

    def _sync_save(self) -> None:
        """Crash-safe GraphML write — called via ``asyncio.to_thread``."""
        with self._save_lock, self._cross_process_lock():
            self._warn_if_superseded()
            self._sync_save_locked()
            self._file_identity = self._current_file_identity()

    def _warn_if_superseded(self) -> None:
        """Report that this save is about to discard another writer's version.

        Each writer rewrites the whole graph, so whoever publishes last wins.
        The lock above makes writes atomic with respect to each other, but it
        cannot reconcile two divergent in-memory graphs — a merge would have to
        choose between resurrecting nodes one side deleted and dropping nodes
        the other side added, and neither is safe to guess.

        Detecting it is still worth doing: silent divergence is how a graph
        quietly loses a day of ingestion, whereas this leaves a trail naming the
        process that overwrote what.
        """
        if self._file_identity is None:
            return
        current = self._current_file_identity()
        if current is not None and current != self._file_identity:
            logger.error(
                "GraphML %s changed under us (pid=%s); this save overwrites the "
                "other writer's version. Run one writer per tenant, or accept "
                "last-write-wins.",
                self.graphml_path,
                os.getpid(),
            )

    def _sync_save_locked(self) -> None:
        """Write one temporary GraphML and atomically publish it."""
        target = self.graphml_path
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        descriptor_open = True
        try:
            with os.fdopen(file_descriptor, "wb") as temp_file:
                descriptor_open = False
                nx.write_graphml(self._graph, temp_file, encoding="utf-8")
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_path, target)
            self._fsync_directory(target.parent)
        finally:
            if descriptor_open:
                with contextlib.suppress(OSError):
                    os.close(file_descriptor)
            with contextlib.suppress(OSError):
                temp_path.unlink()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort directory fsync so the rename survives a power loss."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(directory, flags)
        except OSError:
            return
        try:
            with contextlib.suppress(OSError):
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def load(self) -> None:
        """Load the graph from the GraphML file into memory.

        A missing file yields an empty graph. A corrupt one raises — see
        ``_sync_load``.
        """
        await asyncio.to_thread(self._sync_load)
        self._loaded = True
        self.invalidate_path_projection()

    def _sync_load(self) -> None:
        """Synchronous GraphML read — called via asyncio.to_thread.

        A corrupt file used to degrade to an empty graph. That is the worst
        possible response here: the caller continues against an empty graph and
        the next ``save`` writes those zero nodes back over the only copy, so a
        recoverable parse error becomes permanent data loss. The bad file is now
        moved aside and the error propagates.
        """
        self._file_identity = self._current_file_identity()
        if not self.graphml_path.exists():
            self._graph = nx.MultiDiGraph()
            return
        try:
            loaded = nx.read_graphml(self.graphml_path)
            # read_graphml returns DiGraph or MultiDiGraph depending on edge keys
            if not isinstance(loaded, nx.MultiDiGraph):
                # Convert DiGraph to MultiDiGraph
                multi = nx.MultiDiGraph()
                multi.add_nodes_from(loaded.nodes(data=True))
                for u, v, data in loaded.edges(data=True):
                    key = data.get("relation", "relation")
                    multi.add_edge(u, v, key=key, **data)
                self._graph = multi
            else:
                self._graph = loaded
        except Exception as exc:
            quarantined = self._quarantine_corrupt_file()
            logger.error(
                "Corrupted GraphML %s moved aside to %s: %s",
                self.graphml_path,
                quarantined,
                exc,
            )
            raise GraphStoreCorruptError(self.graphml_path, quarantined) from exc

    def _quarantine_corrupt_file(self) -> Path | None:
        """Rename an unreadable GraphML aside so its bytes stay recoverable."""
        target = self.graphml_path.with_name(f"{GRAPHML_FILENAME}.corrupt.{time.time_ns()}")
        try:
            os.replace(self.graphml_path, target)
        except OSError as exc:
            logger.error("Could not quarantine %s: %s", self.graphml_path, exc)
            return None
        return target

    async def has_graph(self) -> bool:
        """Check whether the GraphML file exists and is non-empty.

        Returns:
            True if a valid graph file exists.
        """
        return self.graphml_path.exists() and self.graphml_path.stat().st_size > 0

    # ------------------------------------------------------------------
    # Attribute conversion helpers
    # ------------------------------------------------------------------

    def _attrs_to_node(self, node_id: str, attrs: dict[str, Any]) -> GraphNode:
        """Convert NetworkX node attributes to a GraphNode dataclass."""
        source_ids = _str_to_list(attrs.get("source_ids", "[]"))
        recording_ids_raw = _str_to_list(attrs.get("recording_ids", "[]"))
        recording_ids = [int(r) for r in recording_ids_raw if r is not None]

        return GraphNode(
            entity_id=str(node_id),
            name=attrs.get("name", str(node_id)),
            type=attrs.get("type", "未知"),
            description=attrs.get("description", ""),
            source_ids=[str(s) for s in source_ids],
            recording_ids=recording_ids,
            degree=int(attrs.get("degree", 0)),
        )

    def _attrs_to_edge(
        self, source: str, target: str, key: str, attrs: dict[str, Any]
    ) -> GraphEdge:
        """Convert NetworkX edge attributes to a GraphEdge dataclass."""
        confidence_str = attrs.get("confidence", "AMBIGUOUS")
        # Ensure valid EdgeConfidence value (M9 L7 — DEPRECATED round-trips).
        if confidence_str not in ("EXTRACTED", "INFERRED", "AMBIGUOUS", "DEPRECATED"):
            confidence_str = "AMBIGUOUS"
        confidence = cast_edge_confidence(confidence_str)

        score = attrs.get("confidence_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = None

        source_ids = _str_to_list(attrs.get("source_ids", "[]"))

        return GraphEdge(
            source=str(source),
            target=str(target),
            relation=str(key),
            weight=float(attrs.get("weight", 1.0)),
            confidence=confidence,
            confidence_score=score,
            source_ids=[str(s) for s in source_ids],
        )

    @staticmethod
    def _compute_score(confidence: str, weight: float) -> float | None:
        """Compute confidence_score from confidence tag and weight."""
        if confidence == "EXTRACTED":
            return 1.0
        if confidence == "INFERRED":
            return round(weight / (weight + 1.0), 4) if weight > 0 else 0.5
        return None

    async def _ensure_loaded(self) -> None:
        """Lazy-load from disk on first access."""
        if not self._loaded:
            await self.load()


__all__ = ["GraphStoreCorruptError", "NetworkXGraphStore"]
