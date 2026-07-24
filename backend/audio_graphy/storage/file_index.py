"""working_dir JSON file index — VideoRAG-style KV stores + LLM response cache.

Manages ``working_dir/{tenant_id}/`` JSON KV files:
    - kv_store_video_segments.json  — segment-level transcripts
    - kv_store_text_chunks.json     — chunk + provenance
    - kv_store_llm_response_cache.json — LLM response cache (saves tokens)
    - kv_store_video_path.json      — recording → path

Model: accumulate in memory → ``flush()`` writes all stores to disk (checkpoint).
Operations are serialised per ``FileIndex`` instance, and each store is
published atomically with a same-directory temporary file + rename.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from audio_graphy.core.types import StorageError

logger = logging.getLogger(__name__)

# Well-known store names (DESIGN.md §7.2)
STORE_VIDEO_SEGMENTS = "kv_store_video_segments"
STORE_TEXT_CHUNKS = "kv_store_text_chunks"
STORE_LLM_RESPONSE_CACHE = "kv_store_llm_response_cache"
STORE_VIDEO_PATH = "kv_store_video_path"


class FileIndex:
    """working_dir JSON KV file index with LLM response cache.

    All file I/O is wrapped in ``asyncio.to_thread`` to avoid blocking the
    event loop (no aiofiles dependency needed).

    Args:
        working_dir: Root working_dir path.
        tenant_id: Tenant ID (determines sub-directory path).
    """

    def __init__(self, working_dir: Path, *, tenant_id: str = "default") -> None:
        self._working_dir = Path(working_dir)
        self._tenant_id = tenant_id
        self._stores: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def working_path(self) -> Path:
        """The actual tenant sub-directory: ``working_dir/{tenant_id}/``."""
        return self._working_dir / self._tenant_id

    # ------------------------------------------------------------------
    # Generic KV operations
    # ------------------------------------------------------------------

    async def get(self, store_name: str, key: str) -> Any | None:
        """Get a value from a named KV store.

        Args:
            store_name: Logical store name (e.g. ``kv_store_video_segments``).
            key: Lookup key.

        Returns:
            The stored value, or None if not found.
        """
        async with self._lock:
            await self._ensure_loaded()
            store = self._stores.get(store_name, {})
            return store.get(key)

    async def set(self, store_name: str, key: str, value: Any) -> None:
        """Set a key-value pair in a named KV store (in-memory only).

        Call ``flush()`` to persist to disk.

        Args:
            store_name: Logical store name. Auto-created if not exists.
            key: Lookup key.
            value: Any JSON-serialisable value.
        """
        async with self._lock:
            await self._ensure_loaded()
            if store_name not in self._stores:
                self._stores[store_name] = {}
            self._stores[store_name][key] = value

    async def get_all(self, store_name: str) -> dict[str, Any]:
        """Return the entire store as a dict (shallow copy).

        Args:
            store_name: Logical store name.

        Returns:
            A dict of all key-value pairs (empty if store doesn't exist).
        """
        async with self._lock:
            await self._ensure_loaded()
            return dict(self._stores.get(store_name, {}))

    async def delete(self, store_name: str, key: str) -> bool:
        """Delete a key from a store.

        Args:
            store_name: Logical store name.
            key: Key to delete.

        Returns:
            True if the key existed and was deleted, False otherwise.
        """
        async with self._lock:
            await self._ensure_loaded()
            store = self._stores.get(store_name, {})
            if key in store:
                del store[key]
                return True
            return False

    # ------------------------------------------------------------------
    # LLM cache specific
    # ------------------------------------------------------------------

    async def get_llm_cache(self, cache_key: str) -> str | None:
        """Look up a cached LLM response by cache_key.

        Args:
            cache_key: MD5 hash of (model, messages).

        Returns:
            Cached response text, or None if not cached.
        """
        async with self._lock:
            await self._ensure_loaded()
            store = self._stores.get(STORE_LLM_RESPONSE_CACHE, {})
            value = store.get(cache_key)
            if isinstance(value, str):
                return value
            return None

    async def set_llm_cache(self, cache_key: str, response_text: str) -> None:
        """Store an LLM response in the cache (in-memory; flush to persist).

        Args:
            cache_key: MD5 hash of (model, messages).
            response_text: The LLM response text to cache.
        """
        async with self._lock:
            await self._ensure_loaded()
            if STORE_LLM_RESPONSE_CACHE not in self._stores:
                self._stores[STORE_LLM_RESPONSE_CACHE] = {}
            self._stores[STORE_LLM_RESPONSE_CACHE][cache_key] = response_text

    async def llm_cache_hit(self, cache_key: str) -> bool:
        """Check whether a cache_key exists in the LLM response cache.

        Args:
            cache_key: MD5 hash of (model, messages).

        Returns:
            True if the cache_key exists.
        """
        cached = await self.get_llm_cache(cache_key)
        return cached is not None

    # ------------------------------------------------------------------
    # Privacy erasure
    # ------------------------------------------------------------------

    async def erase_recording(self, recording_id: int) -> dict[str, int]:
        """Durably erase file-index data derived from one recording.

        Segment and chunk entries are matched by their canonical
        ``"{recording_id}_..."`` key or by a ``recording_id`` value in their
        metadata. The video-path entry is deleted by exact key.

        LLM cache keys do not carry recording provenance, so the entire cache
        for this ``FileIndex`` tenant is cleared. This is intentionally
        privacy-first: retaining an opaque cache response could retain derived
        personal data after a DSAR or retention deletion.

        The mutation and checkpoint are performed under the instance lock.
        Callers do not need to call :meth:`flush` afterwards.

        Args:
            recording_id: Positive database ID of the recording to erase.

        Returns:
            Per-store deletion counts plus a ``total`` count.

        Raises:
            ValueError: If ``recording_id`` is not a positive integer.
            StorageError: If the durable checkpoint fails.
        """
        if not isinstance(recording_id, int) or isinstance(recording_id, bool) or recording_id <= 0:
            raise ValueError("recording_id must be a positive integer")

        target_id = str(recording_id)
        target_prefix = f"{target_id}_"

        async with self._lock:
            await self._ensure_loaded()

            counts = {
                STORE_VIDEO_SEGMENTS: self._erase_derived_entries(
                    STORE_VIDEO_SEGMENTS,
                    target_id=target_id,
                    target_prefix=target_prefix,
                ),
                STORE_TEXT_CHUNKS: self._erase_derived_entries(
                    STORE_TEXT_CHUNKS,
                    target_id=target_id,
                    target_prefix=target_prefix,
                ),
                STORE_VIDEO_PATH: 0,
                STORE_LLM_RESPONSE_CACHE: 0,
            }

            video_paths = self._stores.get(STORE_VIDEO_PATH, {})
            if target_id in video_paths:
                del video_paths[target_id]
                counts[STORE_VIDEO_PATH] = 1

            llm_cache = self._stores.get(STORE_LLM_RESPONSE_CACHE, {})
            counts[STORE_LLM_RESPONSE_CACHE] = len(llm_cache)
            llm_cache.clear()

            counts["total"] = sum(counts.values())
            await self._flush_unlocked()
            return counts

    def _erase_derived_entries(
        self,
        store_name: str,
        *,
        target_id: str,
        target_prefix: str,
    ) -> int:
        """Remove entries belonging to ``target_id`` from a derived store."""
        store = self._stores.get(store_name, {})
        matching_keys = [
            key
            for key, value in store.items()
            if key.startswith(target_prefix)
            or (isinstance(value, dict) and str(value.get("recording_id", "")) == target_id)
        ]
        for key in matching_keys:
            del store[key]
        return len(matching_keys)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Write all in-memory KV stores to disk (checkpoint).

        Raises:
            StorageError: If disk write fails.
        """
        async with self._lock:
            await self._flush_unlocked()

    async def _flush_unlocked(self) -> None:
        """Checkpoint stores; caller must hold ``self._lock``."""
        self.working_path.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._sync_flush)
        except OSError as exc:
            raise StorageError(f"Failed to flush file_index to {self.working_path}: {exc}") from exc
        logger.debug("Flushed %d KV stores to %s", len(self._stores), self.working_path)

    def _sync_flush(self) -> None:
        """Synchronous flush — called via asyncio.to_thread."""
        for store_name, data in self._stores.items():
            path = self.working_path / f"{store_name}.json"
            temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                temporary_path.replace(path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary_path.unlink()

    async def load(self) -> None:
        """Load all KV stores from disk into memory.

        Missing or corrupted JSON files are silently replaced with empty dicts
        (per PRD §4.8 error handling: initialise empty, don't block).
        """
        async with self._lock:
            await asyncio.to_thread(self._sync_load)
            self._loaded = True

    def _sync_load(self) -> None:
        """Synchronous load — called via asyncio.to_thread."""
        self._stores.clear()
        if not self.working_path.exists():
            return

        for json_file in self.working_path.glob("*.json"):
            store_name = json_file.stem
            try:
                text = json_file.read_text(encoding="utf-8")
                self._stores[store_name] = json.loads(text) if text.strip() else {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Corrupted JSON %s, initialising empty store: %s", json_file, exc)
                self._stores[store_name] = {}

    async def _ensure_loaded(self) -> None:
        """Lazy-load from disk; caller must hold ``self._lock``."""
        if not self._loaded:
            await asyncio.to_thread(self._sync_load)
            self._loaded = True
