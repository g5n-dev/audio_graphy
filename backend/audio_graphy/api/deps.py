"""Shared FastAPI dependencies.

Provides:
    - ``get_db``: async session generator.
    - ``get_current_user``: AuthUser from request state (+ DB name/email lookup).
    - ``get_jwt_manager``: JWTManager from app state.
    - ``get_adapters``: AdapterBundle from app state.
    - ``get_session_factory``: async_sessionmaker from app state.
    - ``get_vector_store``: MySQLVectorStore from app state.
    - ``get_graph_store``: per-tenant NetworkXGraphStore (lazy-loaded + cached).
    - ``get_file_index``: per-tenant FileIndex (lazy-loaded + cached).
    - ``StoreBundle``: dataclass bundling vector_store + graph_store + file_index.

See: docs/m3-architecture.md §3.3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.auth.middleware import AuthUser
from audio_graphy.errors import InvalidTokenError

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.auth.jwt_utils import JWTManager
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.mysql_vector import MySQLVectorStore


@dataclass(frozen=True, slots=True)
class StoreBundle:
    """Per-tenant store bundle returned by ``get_stores``.

    Attributes:
        vector_store: Global MySQLVectorStore (filters by tenant_id internally).
        graph_store: Per-tenant NetworkXGraphStore (lazy-loaded).
        file_index: Per-tenant FileIndex (lazy-loaded).
    """

    vector_store: MySQLVectorStore
    graph_store: NetworkXGraphStore
    file_index: FileIndex


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Get the async session factory from app state."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise InvalidTokenError("Database session factory not initialized")
    return factory  # type: ignore[no-any-return]


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an async DB session from the app-level session factory."""
    factory = get_session_factory(request)
    async with factory() as session:
        yield session


def get_jwt_manager(request: Request) -> JWTManager:
    """Get the JWTManager from app state."""
    manager = getattr(request.app.state, "jwt_manager", None)
    if manager is None:
        raise InvalidTokenError("JWT manager not initialized")
    return manager  # type: ignore[no-any-return]


def get_adapters(request: Request) -> AdapterBundle:
    """Get the AdapterBundle from app state."""
    bundle = getattr(request.app.state, "adapter_bundle", None)
    if bundle is None:
        raise InvalidTokenError("Adapter bundle not initialized")
    return bundle  # type: ignore[no-any-return]


def get_vector_store(request: Request) -> MySQLVectorStore:
    """Get the global MySQLVectorStore from app state."""
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise InvalidTokenError("Vector store not initialized")
    return store  # type: ignore[no-any-return]


def _get_graph_store(request: Request) -> NetworkXGraphStore:
    """Get or create a per-tenant NetworkXGraphStore (cached on app state)."""
    tenant_id = getattr(request.state, "tenant_id", "default")
    graph_stores: dict[str, NetworkXGraphStore] | None = getattr(
        request.app.state, "graph_stores", None
    )
    if graph_stores is None:
        graph_stores = {}
        request.app.state.graph_stores = graph_stores

    store = graph_stores.get(tenant_id)
    if store is None:
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        working_dir = request.app.state.settings.working_dir
        store = NetworkXGraphStore(working_dir, tenant_id=tenant_id)
        graph_stores[tenant_id] = store
    return store


def _get_file_index(request: Request) -> FileIndex:
    """Get or create a per-tenant FileIndex (cached on app state)."""
    tenant_id = getattr(request.state, "tenant_id", "default")
    file_indexes: dict[str, FileIndex] | None = getattr(request.app.state, "file_indexes", None)
    if file_indexes is None:
        file_indexes = {}
        request.app.state.file_indexes = file_indexes

    index = file_indexes.get(tenant_id)
    if index is None:
        from audio_graphy.storage.file_index import FileIndex

        working_dir = request.app.state.settings.working_dir
        index = FileIndex(working_dir, tenant_id=tenant_id)
        file_indexes[tenant_id] = index
    return index


def get_graph_store(request: Request) -> NetworkXGraphStore:
    """Public dependency: get per-tenant graph store."""
    return _get_graph_store(request)


def get_file_index(request: Request) -> FileIndex:
    """Public dependency: get per-tenant file index."""
    return _get_file_index(request)


def get_stores(request: Request) -> StoreBundle:
    """Get a per-tenant StoreBundle (vector_store + graph_store + file_index)."""
    return StoreBundle(
        vector_store=get_vector_store(request),
        graph_store=_get_graph_store(request),
        file_index=_get_file_index(request),
    )


async def get_current_user(request: Request) -> AuthUser:
    """Get the authenticated user from request state.

    The middleware injects a minimal AuthUser (id/role/tenant_id only).
    This dependency enriches it with name/email by querying the DB.

    For performance, if name/email are already set (e.g., test fixtures),
    the DB lookup is skipped.
    """
    user: AuthUser | None = getattr(request.state, "user", None)
    if user is None:
        raise InvalidTokenError("No authenticated user")

    # If name is empty, enrich from DB
    if not user.name:
        factory = get_session_factory(request)
        from sqlalchemy import select

        from audio_graphy.models.user import User

        async with factory() as session:
            result = await session.execute(
                select(User).where(
                    User.id == user.id,
                    User.tenant_id == user.tenant_id,
                )
            )
            db_user = result.scalar_one_or_none()
            if db_user is not None:
                user = AuthUser(
                    id=db_user.id,
                    name=str(db_user.name),
                    email=str(db_user.email),
                    role=str(db_user.role),
                    tenant_id=str(db_user.tenant_id),
                )
                request.state.user = user
                # Set agent_filter for agent role
                if user.role == "agent":
                    request.state.agent_filter = user.name

    return user
