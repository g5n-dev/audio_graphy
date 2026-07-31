"""Real Redis integration coverage for hot-cache outage and recovery."""

from __future__ import annotations

import asyncio
import shutil
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from redis.asyncio import Redis

from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.storage.llm_hot_cache import (
    CacheIdentity,
    FailoverHotCache,
    HotCacheValue,
    LocalHotCache,
    RedisHotCache,
)


class _RedisProcess:
    def __init__(self, executable: str, directory: Path) -> None:
        self._executable = executable
        self._directory = directory
        self.port = _unused_port()
        self.process: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/15"

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Redis test process is already running")
        self.process = await asyncio.create_subprocess_exec(
            self._executable,
            "--bind",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--protected-mode",
            "no",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(self._directory),
            "--logfile",
            str(self._directory / "redis.log"),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        client = Redis.from_url(
            self.url,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
        )
        try:
            for _ in range(100):
                if self.process.returncode is not None:
                    raise RuntimeError("redis-server exited during startup")
                try:
                    if await client.ping():
                        return
                except Exception:
                    await asyncio.sleep(0.02)
            raise TimeoutError("redis-server did not become ready")
        finally:
            await client.aclose()

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=3)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest_asyncio.fixture
async def real_redis(tmp_path: Path) -> AsyncIterator[_RedisProcess]:
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    server = _RedisProcess(executable, tmp_path)
    try:
        await server.start()
    except Exception as exc:
        await server.stop()
        pytest.skip(f"redis-server cannot start: {exc}")
    try:
        yield server
    finally:
        await server.stop()


@pytest.mark.integration
async def test_real_redis_ttl_size_outage_and_two_probe_recovery(
    real_redis: _RedisProcess,
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(Fernet.generate_key())
    crypto = LLMCacheCrypto(key_path, max_plaintext_bytes=4096)
    local = LocalHotCache(max_entries=8, max_bytes=8192, max_item_bytes=1024)
    cache = FailoverHotCache(
        RedisHotCache(
            real_redis.url,
            crypto=crypto,
            max_item_bytes=1024,
            max_ttl_seconds=2,
            socket_timeout_seconds=0.1,
        ),
        local,
        failure_threshold=3,
        circuit_seconds=0.05,
        recovery_successes=2,
        probe_interval_seconds=3600,
    )
    identity = CacheIdentity("tenant-a", "keyword_extract", "a" * 64)
    value = HotCacheValue(b"sensitive validated output", True)
    await cache.start()

    try:
        assert cache.backend_name == "redis"
        assert await cache.set(identity, value, ttl_seconds=60)
        inspector = Redis.from_url(real_redis.url, decode_responses=False)
        try:
            encrypted = await inspector.get(identity.redis_key)
            assert isinstance(encrypted, bytes)
            assert value.payload not in encrypted
            assert 0 < await inspector.ttl(identity.redis_key) <= 2
        finally:
            await inspector.aclose()
        assert not await cache.set(
            CacheIdentity("tenant-a", "keyword_extract", "b" * 64),
            HotCacheValue(b"x" * 1025, False),
            ttl_seconds=60,
        )

        await real_redis.stop()
        for character in ("c", "d", "e"):
            assert await cache.set(
                CacheIdentity("tenant-a", "keyword_extract", character * 64),
                value,
                ttl_seconds=60,
            )
        assert cache.backend_name == "local"
        assert local.entry_count == 3

        await real_redis.start()
        await asyncio.sleep(0.06)
        assert not await cache.probe_once()
        assert await cache.probe_once()
        assert cache.backend_name == "redis"
        assert local.entry_count == 0
    finally:
        await cache.aclose()
