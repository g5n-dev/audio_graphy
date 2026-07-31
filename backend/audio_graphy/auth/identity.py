"""Authenticated identity resolution.

Turns the minimal ``request.state.user`` injected by ``AuthMiddleware`` into a
fully populated :class:`AuthUser`, re-validated against the authoritative
``users`` row. Lives in ``auth`` so RBAC guards can refresh identity without
depending on the API layer.

See: docs/m3-architecture.md §3.1.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from audio_graphy.auth.middleware import AuthUser
from audio_graphy.errors import InvalidTokenError


async def resolve_current_user(request: Request) -> AuthUser:
    """Get the authenticated user from request state.

    The middleware injects a minimal AuthUser (id/role/tenant_id only).
    This enriches it with name/email by querying the DB.

    For performance, if name/email are already set (e.g., test fixtures),
    the DB lookup is skipped.
    """
    user: AuthUser | None = getattr(request.state, "user", None)
    if user is None:
        raise InvalidTokenError("No authenticated user")

    # If name is empty, enrich from DB
    if not user.name:
        factory: async_sessionmaker[AsyncSession] | None = getattr(
            request.app.state, "session_factory", None
        )
        if factory is None:
            raise InvalidTokenError("Database session factory not initialized")

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
            if db_user is None:
                # JWTs and native-audio playback grants must stop working as
                # soon as the backing account is removed. Never fall back to
                # stale claims when the authoritative row no longer exists.
                raise InvalidTokenError("User no longer exists")
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
