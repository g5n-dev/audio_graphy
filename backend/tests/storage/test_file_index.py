"""Unit tests for FileIndex — working_dir JSON KV stores + LLM cache.

Tests cover:
    - Generic KV CRUD (get/set/get_all/delete)
    - LLM cache specific operations (get/set/hit)
    - flush/load persistence round-trip
    - Error handling (missing dir, corrupted JSON)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_graphy.storage.file_index import (
    STORE_LLM_RESPONSE_CACHE,
    STORE_TEXT_CHUNKS,
    STORE_VIDEO_PATH,
    STORE_VIDEO_SEGMENTS,
    FileIndex,
)


@pytest.mark.unit
class TestFileIndexKV:
    """Generic KV store operations."""

    async def test_set_and_get(self, file_index: FileIndex) -> None:
        """Set a value and retrieve it."""
        await file_index.set("kv_store_test", "key1", {"value": 42})
        result = await file_index.get("kv_store_test", "key1")
        assert result == {"value": 42}

    async def test_get_missing_key(self, file_index: FileIndex) -> None:
        """Getting a non-existent key returns None."""
        result = await file_index.get("kv_store_test", "nonexistent")
        assert result is None

    async def test_get_missing_store(self, file_index: FileIndex) -> None:
        """Getting from a non-existent store returns None."""
        result = await file_index.get("nonexistent_store", "key")
        assert result is None

    async def test_set_creates_store_automatically(self, file_index: FileIndex) -> None:
        """Setting a key in a new store auto-creates it."""
        await file_index.set("new_store", "k", "v")
        result = await file_index.get("new_store", "k")
        assert result == "v"

    async def test_get_all(self, file_index: FileIndex) -> None:
        """get_all returns all key-value pairs in a store."""
        await file_index.set(STORE_VIDEO_SEGMENTS, "seg_0", {"text": "hello"})
        await file_index.set(STORE_VIDEO_SEGMENTS, "seg_1", {"text": "world"})
        all_data = await file_index.get_all(STORE_VIDEO_SEGMENTS)
        assert len(all_data) == 2
        assert all_data["seg_0"]["text"] == "hello"
        assert all_data["seg_1"]["text"] == "world"

    async def test_get_all_empty_store(self, file_index: FileIndex) -> None:
        """get_all on a non-existent store returns empty dict."""
        result = await file_index.get_all("nonexistent")
        assert result == {}

    async def test_delete_existing(self, file_index: FileIndex) -> None:
        """Delete an existing key returns True."""
        await file_index.set(STORE_TEXT_CHUNKS, "chunk_1", {"text": "data"})
        deleted = await file_index.delete(STORE_TEXT_CHUNKS, "chunk_1")
        assert deleted is True
        result = await file_index.get(STORE_TEXT_CHUNKS, "chunk_1")
        assert result is None

    async def test_delete_missing(self, file_index: FileIndex) -> None:
        """Delete a non-existent key returns False."""
        deleted = await file_index.delete(STORE_TEXT_CHUNKS, "nonexistent")
        assert deleted is False

    async def test_overwrite_value(self, file_index: FileIndex) -> None:
        """Setting the same key overwrites the value."""
        await file_index.set("store", "k", "old")
        await file_index.set("store", "k", "new")
        result = await file_index.get("store", "k")
        assert result == "new"


@pytest.mark.unit
class TestFileIndexLLMCache:
    """LLM response cache specific operations."""

    async def test_set_and_get_llm_cache(self, file_index: FileIndex) -> None:
        """Set and retrieve an LLM cached response."""
        await file_index.set_llm_cache("abc123", "cached LLM response text")
        result = await file_index.get_llm_cache("abc123")
        assert result == "cached LLM response text"

    async def test_get_llm_cache_miss(self, file_index: FileIndex) -> None:
        """Cache miss returns None."""
        result = await file_index.get_llm_cache("nonexistent_key")
        assert result is None

    async def test_llm_cache_hit(self, file_index: FileIndex) -> None:
        """llm_cache_hit returns True for cached, False for miss."""
        await file_index.set_llm_cache("key1", "response")
        assert await file_index.llm_cache_hit("key1") is True
        assert await file_index.llm_cache_hit("key2") is False


@pytest.mark.unit
class TestFileIndexRecordingErasure:
    """Recording-scoped privacy erasure."""

    async def test_erases_recording_data_and_tenant_llm_cache_durably(
        self, tmp_working_dir: Path
    ) -> None:
        """Erase all derived data, preserve other recordings, and persist immediately."""
        file_index = FileIndex(tmp_working_dir, tenant_id="tenant_a")

        # Canonical key and legacy/non-canonical key matched through metadata.
        await file_index.set(
            STORE_VIDEO_SEGMENTS,
            "7_0",
            {"recording_id": 7, "transcript": "customer phone number"},
        )
        await file_index.set(
            STORE_VIDEO_SEGMENTS,
            "legacy_segment_key",
            {"recording_id": "7", "transcript": "customer address"},
        )
        await file_index.set(
            STORE_VIDEO_SEGMENTS,
            "70_0",
            {"recording_id": 70, "transcript": "must remain"},
        )

        await file_index.set(
            STORE_TEXT_CHUNKS,
            "7_101",
            {"recording_id": 7, "text": "derived target text"},
        )
        await file_index.set(
            STORE_TEXT_CHUNKS,
            "legacy_chunk_key",
            {"recording_id": "7", "text": "derived legacy target text"},
        )
        await file_index.set(
            STORE_TEXT_CHUNKS,
            "70_102",
            {"recording_id": 70, "text": "must remain"},
        )

        await file_index.set(STORE_VIDEO_PATH, "7", {"recording_id": 7})
        await file_index.set(STORE_VIDEO_PATH, "70", {"recording_id": 70})
        await file_index.set_llm_cache("target-or-untraceable-1", "possibly derived PII")
        await file_index.set_llm_cache("target-or-untraceable-2", "possibly derived PII")
        await file_index.set("unrelated_store", "7_0", {"recording_id": 7})
        await file_index.flush()

        counts = await file_index.erase_recording(7)

        assert counts == {
            STORE_VIDEO_SEGMENTS: 2,
            STORE_TEXT_CHUNKS: 2,
            STORE_VIDEO_PATH: 1,
            STORE_LLM_RESPONSE_CACHE: 2,
            "total": 7,
        }

        # A new instance proves erase_recording persisted the mutation itself.
        reloaded = FileIndex(tmp_working_dir, tenant_id="tenant_a")
        assert await reloaded.get_all(STORE_VIDEO_SEGMENTS) == {
            "70_0": {"recording_id": 70, "transcript": "must remain"}
        }
        assert await reloaded.get_all(STORE_TEXT_CHUNKS) == {
            "70_102": {"recording_id": 70, "text": "must remain"}
        }
        assert await reloaded.get_all(STORE_VIDEO_PATH) == {"70": {"recording_id": 70}}
        assert await reloaded.get_all(STORE_LLM_RESPONSE_CACHE) == {}
        assert await reloaded.get_all("unrelated_store") == {"7_0": {"recording_id": 7}}

    async def test_missing_recording_still_clears_untraceable_llm_cache(
        self, tmp_working_dir: Path
    ) -> None:
        """An absent recording can still have untraceable LLM-derived PII."""
        file_index = FileIndex(tmp_working_dir, tenant_id="tenant_a")
        await file_index.set_llm_cache("opaque-key", "opaque cached response")
        await file_index.flush()

        counts = await file_index.erase_recording(404)

        assert counts == {
            STORE_VIDEO_SEGMENTS: 0,
            STORE_TEXT_CHUNKS: 0,
            STORE_VIDEO_PATH: 0,
            STORE_LLM_RESPONSE_CACHE: 1,
            "total": 1,
        }
        reloaded = FileIndex(tmp_working_dir, tenant_id="tenant_a")
        assert await reloaded.get_all(STORE_LLM_RESPONSE_CACHE) == {}

    @pytest.mark.parametrize("recording_id", [0, -1, True])
    async def test_rejects_invalid_recording_id(
        self, file_index: FileIndex, recording_id: int
    ) -> None:
        """Invalid IDs must not trigger a tenant-wide cache purge."""
        await file_index.set_llm_cache("keep", "response")

        with pytest.raises(ValueError, match="positive integer"):
            await file_index.erase_recording(recording_id)

        assert await file_index.get_llm_cache("keep") == "response"


@pytest.mark.unit
class TestFileIndexPersistence:
    """flush / load persistence round-trip."""

    async def test_flush_creates_json_files(
        self, file_index: FileIndex, tmp_working_dir: Path
    ) -> None:
        """flush() writes JSON files to working_dir/{tenant_id}/."""
        await file_index.set(STORE_VIDEO_SEGMENTS, "seg_0", {"text": "hello"})
        await file_index.set(STORE_LLM_RESPONSE_CACHE, "key1", "response")
        await file_index.flush()

        segments_path = file_index.working_path / f"{STORE_VIDEO_SEGMENTS}.json"
        cache_path = file_index.working_path / f"{STORE_LLM_RESPONSE_CACHE}.json"
        assert segments_path.exists()
        assert cache_path.exists()

    async def test_flush_then_load_roundtrip(self, tmp_working_dir: Path) -> None:
        """Data survives flush → new instance → load round-trip."""
        fi1 = FileIndex(tmp_working_dir, tenant_id="default")
        await fi1.set(STORE_VIDEO_SEGMENTS, "seg_0", {"text": "hello", "time": 1.5})
        await fi1.set_llm_cache("hash123", "LLM response")
        await fi1.flush()

        fi2 = FileIndex(tmp_working_dir, tenant_id="default")
        await fi2.load()
        assert await fi2.get(STORE_VIDEO_SEGMENTS, "seg_0") == {"text": "hello", "time": 1.5}
        assert await fi2.get_llm_cache("hash123") == "LLM response"

    async def test_load_missing_dir(self, tmp_working_dir: Path) -> None:
        """load() on a non-existent directory initialises empty stores."""
        fi = FileIndex(tmp_working_dir, tenant_id="new_tenant")
        await fi.load()
        assert await fi.get("any_store", "any_key") is None

    async def test_load_corrupted_json(self, tmp_working_dir: Path) -> None:
        """Corrupted JSON files are silently replaced with empty dicts."""
        tenant_dir = tmp_working_dir / "default"
        tenant_dir.mkdir(parents=True)
        (tenant_dir / f"{STORE_VIDEO_SEGMENTS}.json").write_text("{invalid json", encoding="utf-8")

        fi = FileIndex(tmp_working_dir, tenant_id="default")
        await fi.load()
        # Should not crash — returns empty
        assert await fi.get(STORE_VIDEO_SEGMENTS, "key") is None

    async def test_working_path_property(
        self, file_index: FileIndex, tmp_working_dir: Path
    ) -> None:
        """working_path returns working_dir/{tenant_id}/."""
        expected = tmp_working_dir / "default"
        assert file_index.working_path == expected

    async def test_tenant_isolation(self, tmp_working_dir: Path) -> None:
        """Different tenants have separate KV stores."""
        fi_a = FileIndex(tmp_working_dir, tenant_id="tenant_a")
        fi_b = FileIndex(tmp_working_dir, tenant_id="tenant_b")

        await fi_a.set("store", "key", "value_a")
        await fi_b.set("store", "key", "value_b")
        await fi_a.flush()
        await fi_b.flush()

        assert await fi_a.get("store", "key") == "value_a"
        assert await fi_b.get("store", "key") == "value_b"

        # File paths are different
        assert fi_a.working_path != fi_b.working_path
