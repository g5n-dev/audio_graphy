"""RBAC role guard — FastAPI dependency for role-based access control.

Provides ``require_role(*roles)`` which returns a FastAPI dependency
that checks ``request.state.user.role`` against the allowed roles.

See: docs/m3-architecture.md §3.1, docs/m3-prd.md AUTH-04, §5 RBAC Matrix.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request

from audio_graphy.auth.identity import resolve_current_user
from audio_graphy.errors import ForbiddenError

# Role hierarchy levels (higher = more privileged)
_ROLE_LEVELS: dict[str, int] = {
    "viewer": 1,
    "agent": 2,
    "inspector": 3,
    "admin": 4,
}


def get_role_level(role: str) -> int:
    """Return the numeric privilege level for a role."""
    return _ROLE_LEVELS.get(role, 0)


def require_role(*roles: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Create a FastAPI dependency that enforces role-based access.

    Usage::

        @router.post("/recordings", dependencies=[Depends(require_role("admin"))])

    Args:
        roles: Allowed role strings (e.g. "admin", "inspector").

    Returns:
        A FastAPI-compatible async dependency function.
    """
    allowed = set(roles)

    async def _check(request: Request) -> None:
        # Refresh identity and role from the authoritative user row before
        # authorization. This makes account deletion and role downgrade take
        # effect immediately instead of trusting a long-lived JWT claim.
        user = await resolve_current_user(request)
        if user.role not in allowed:
            raise ForbiddenError(
                message=f"Role '{user.role}' is not allowed. Required: {', '.join(sorted(allowed))}",
                detail={"required_roles": sorted(allowed), "actual_role": user.role},
            )

    return _check


def require_admin() -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency that requires admin role."""
    return require_role("admin")


def require_inspector_or_above() -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency that requires inspector or admin role."""
    return require_role("admin", "inspector")


def require_write_access() -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency that requires admin or inspector (write-level access)."""
    return require_role("admin", "inspector")


def require_any_authenticated() -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency that allows any authenticated user."""
    return require_role("admin", "inspector", "agent", "viewer")
