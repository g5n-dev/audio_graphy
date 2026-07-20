"""Tenant isolation helpers — query filters for multi-tenant row-level security.

Provides ``apply_tenant_filter`` and ``apply_agent_filter`` that augment
SQLAlchemy ``select`` statements with ``WHERE tenant_id = ?`` (and optionally
``WHERE agent_name = ?`` for agent-role scoping).

See: docs/m3-architecture.md §7.4, §7.5, docs/m3-prd.md AUTH-03/05/06.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import Select
from sqlalchemy.orm import InstrumentedAttribute


def apply_tenant_filter(
    stmt: Select[Any],
    model_cls: type,
    tenant_id: str,
) -> Select[Any]:
    """Add ``WHERE model.tenant_id = tenant_id`` to a select statement.

    Args:
        stmt: The SQLAlchemy select statement.
        model_cls: The ORM model class (must have ``tenant_id`` column).
        tenant_id: The tenant ID to filter by.

    Returns:
        The filtered select statement.
    """
    tenant_col: InstrumentedAttribute[Any] = model_cls.tenant_id  # type: ignore[attr-defined]
    return stmt.where(tenant_col == tenant_id)


def apply_agent_filter(
    stmt: Select[Any],
    model_cls: type,
    agent_name: str,
) -> Select[Any]:
    """Add ``WHERE model.agent_name = agent_name`` to a select statement.

    Only applies to models that have an ``agent_name`` column.

    Args:
        stmt: The SQLAlchemy select statement.
        model_cls: The ORM model class (must have ``agent_name`` column).
        agent_name: The agent name to filter by.

    Returns:
        The filtered select statement.
    """
    agent_col: InstrumentedAttribute[Any] | None = getattr(model_cls, "agent_name", None)
    if agent_col is not None:
        stmt = stmt.where(agent_col == agent_name)
    return stmt


def get_tenant_id(request: Request) -> str:
    """Extract tenant_id from request state (set by AuthMiddleware).

    Args:
        request: The FastAPI request.

    Returns:
        The tenant ID string.

    Raises:
        InvalidTokenError: If tenant_id is not set (middleware not configured).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        from audio_graphy.errors import InvalidTokenError

        raise InvalidTokenError("No tenant_id on request (middleware misconfiguration)")
    return str(tenant_id)


def get_agent_filter(request: Request) -> str | None:
    """Extract agent_filter from request state (set by AuthMiddleware for agent role).

    Returns None for non-agent roles.

    Args:
        request: The FastAPI request.

    Returns:
        The agent name string for agent role, or None.
    """
    return getattr(request.state, "agent_filter", None)


def apply_tenant_and_agent_filters(
    stmt: Select[Any],
    model_cls: type,
    request: Request,
) -> Select[Any]:
    """Apply both tenant_id and agent_filter (if applicable) to a select statement.

    Convenience function that reads from request state.

    Args:
        stmt: The SQLAlchemy select statement.
        model_cls: The ORM model class.
        request: The FastAPI request (contains tenant_id + agent_filter).

    Returns:
        The filtered select statement.
    """
    tenant_id = get_tenant_id(request)
    stmt = apply_tenant_filter(stmt, model_cls, tenant_id)
    agent_filter = get_agent_filter(request)
    if agent_filter is not None:
        stmt = apply_agent_filter(stmt, model_cls, agent_filter)
    return stmt
