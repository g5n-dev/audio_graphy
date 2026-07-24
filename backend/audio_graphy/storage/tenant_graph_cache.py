"""Thread-safe bounded LRU for per-tenant in-memory graph stores."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _LoadLockRef:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class TenantGraphStoreCache[T](MutableMapping[str, T]):
    """Bounded tenant LRU with single-flight lock metadata tied to entries."""

    def __init__(self, *, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._stores: OrderedDict[str, T] = OrderedDict()
        self._load_locks: dict[str, _LoadLockRef] = {}
        self._guard = threading.RLock()
        self.evictions = 0

    @property
    def max_entries(self) -> int:
        """Configured maximum resident tenant graphs."""

        return self._max_entries

    @property
    def load_lock_count(self) -> int:
        """Number of lock metadata records retained by cached or active tenants."""

        with self._guard:
            return len(self._load_locks)

    def __getitem__(self, tenant_id: str) -> T:
        with self._guard:
            store = self._stores[tenant_id]
            self._stores.move_to_end(tenant_id)
            return store

    def __setitem__(self, tenant_id: str, store: T) -> None:
        with self._guard:
            self._stores[tenant_id] = store
            self._stores.move_to_end(tenant_id)
            while len(self._stores) > self._max_entries:
                evicted_tenant, _ = self._stores.popitem(last=False)
                self.evictions += 1
                self._cleanup_lock_if_unused(evicted_tenant)

    def __delitem__(self, tenant_id: str) -> None:
        with self._guard:
            del self._stores[tenant_id]
            self._cleanup_lock_if_unused(tenant_id)

    def __iter__(self) -> Iterator[str]:
        with self._guard:
            return iter(tuple(self._stores))

    def __len__(self) -> int:
        with self._guard:
            return len(self._stores)

    def clear(self) -> None:
        """Atomically drop all stores and any inactive lock metadata."""

        with self._guard:
            self._stores.clear()
            for tenant_id in tuple(self._load_locks):
                self._cleanup_lock_if_unused(tenant_id)

    def snapshot_items(self) -> tuple[tuple[str, T], ...]:
        """Return a stable LRU-ordered snapshot without refreshing recency."""

        with self._guard:
            return tuple(self._stores.items())

    def discard_if_same(self, tenant_id: str, store: object) -> bool:
        """Remove ``tenant_id`` only when it still points at ``store``."""

        with self._guard:
            current = self._stores.get(tenant_id)
            if current is not store:
                return False
            del self._stores[tenant_id]
            self._cleanup_lock_if_unused(tenant_id)
            return True

    @contextmanager
    def load_guard(self, tenant_id: str) -> Iterator[None]:
        """Serialize cold loads for a tenant and lifecycle its lock record."""

        with self._guard:
            ref = self._load_locks.get(tenant_id)
            if ref is None:
                ref = _LoadLockRef()
                self._load_locks[tenant_id] = ref
            ref.users += 1

        ref.lock.acquire()
        try:
            yield
        finally:
            ref.lock.release()
            with self._guard:
                ref.users -= 1
                if ref.users == 0 and tenant_id not in self._stores:
                    current = self._load_locks.get(tenant_id)
                    if current is ref:
                        self._load_locks.pop(tenant_id, None)

    def _cleanup_lock_if_unused(self, tenant_id: str) -> None:
        ref = self._load_locks.get(tenant_id)
        if ref is not None and ref.users == 0:
            self._load_locks.pop(tenant_id, None)


__all__ = ["TenantGraphStoreCache"]
