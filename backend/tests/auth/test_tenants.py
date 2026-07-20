"""Unit tests for auth/tenants.py — tenant isolation query helpers.

Tests: apply_tenant_filter, apply_agent_filter, get_tenant_id,
get_agent_filter, apply_tenant_and_agent_filters.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import Request

from audio_graphy.auth.tenants import (
    apply_agent_filter,
    apply_tenant_and_agent_filters,
    apply_tenant_filter,
    get_agent_filter,
    get_tenant_id,
)
from audio_graphy.models.recording import Recording


class TestApplyTenantFilter:
    """Tests for apply_tenant_filter."""

    def test_adds_tenant_where_clause(self) -> None:
        """apply_tenant_filter adds WHERE tenant_id = ?."""
        from sqlalchemy import select

        stmt = select(Recording)
        filtered = apply_tenant_filter(stmt, Recording, "chang_an")
        # The compiled SQL should contain tenant_id
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "tenant_id" in compiled
        assert "chang_an" in compiled


class TestApplyAgentFilter:
    """Tests for apply_agent_filter."""

    def test_adds_agent_where_clause(self) -> None:
        """apply_agent_filter adds WHERE agent_name = ? for models with agent_name."""
        from sqlalchemy import select

        stmt = select(Recording)
        filtered = apply_agent_filter(stmt, Recording, "张敏")
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "agent_name" in compiled

    def test_noop_for_model_without_agent_name(self) -> None:
        """apply_agent_filter is a no-op for models without agent_name column."""
        from sqlalchemy import select

        # Use a model without agent_name (e.g. Tenant)
        from audio_graphy.models.tenant import Tenant

        stmt = select(Tenant)
        filtered = apply_agent_filter(stmt, Tenant, "张敏")
        # Should not add any WHERE for agent_name
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "agent_name" not in compiled


class TestGetTenantId:
    """Tests for get_tenant_id."""

    def test_returns_tenant_id_from_request(self) -> None:
        """get_tenant_id returns the tenant_id from request.state."""
        request = MagicMock(spec=Request)
        request.state.tenant_id = "chang_an"
        result = get_tenant_id(request)
        assert result == "chang_an"

    def test_raises_when_no_tenant_id(self) -> None:
        """get_tenant_id raises InvalidTokenError when tenant_id is missing."""
        from audio_graphy.errors import InvalidTokenError

        request = MagicMock(spec=Request)
        # MagicMock returns a MagicMock for any attr, so we need to explicitly set None
        del request.state.tenant_id
        request.state.tenant_id = None
        with pytest.raises(InvalidTokenError):
            get_tenant_id(request)


class TestGetAgentFilter:
    """Tests for get_agent_filter."""

    def test_returns_agent_filter_from_request(self) -> None:
        """get_agent_filter returns the agent_filter from request.state."""
        request = MagicMock(spec=Request)
        request.state.agent_filter = "张敏"
        result = get_agent_filter(request)
        assert result == "张敏"

    def test_returns_none_when_no_agent_filter(self) -> None:
        """get_agent_filter returns None when agent_filter is not set."""
        request = MagicMock(spec=Request)
        request.state.agent_filter = None
        result = get_agent_filter(request)
        assert result is None


class TestApplyTenantAndAgentFilters:
    """Tests for apply_tenant_and_agent_filters."""

    def test_applies_both_filters(self) -> None:
        """apply_tenant_and_agent_filters applies tenant + agent filters."""
        from sqlalchemy import select

        request = MagicMock(spec=Request)
        request.state.tenant_id = "chang_an"
        request.state.agent_filter = "张敏"

        stmt = select(Recording)
        filtered = apply_tenant_and_agent_filters(stmt, Recording, request)
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "tenant_id" in compiled
        assert "agent_name" in compiled

    def test_applies_only_tenant_when_no_agent(self) -> None:
        """apply_tenant_and_agent_filters applies only tenant filter when no agent."""
        from sqlalchemy import select

        request = MagicMock(spec=Request)
        request.state.tenant_id = "chang_an"
        request.state.agent_filter = None

        stmt = select(Recording)
        filtered = apply_tenant_and_agent_filters(stmt, Recording, request)
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
        assert "tenant_id" in compiled
        # agent_name should not appear in the WHERE clause (only in column list)
        where_clause = compiled.split("WHERE")[-1] if "WHERE" in compiled else ""
        assert "agent_name" not in where_clause
