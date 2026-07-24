"""Stable agent-identity resolution shared by recording and reception ingestion."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.models.user import User

logger = logging.getLogger(__name__)


async def resolve_unique_agent_user_id(
    session: AsyncSession,
    *,
    tenant_id: str,
    agent_name: str | None,
) -> int | None:
    """Resolve one tenant-local agent name, failing closed on zero or duplicates."""

    if agent_name is None:
        return None
    result = await session.execute(
        select(User.id)
        .where(
            User.tenant_id == tenant_id,
            User.name == agent_name,
            User.role == "agent",
        )
        .order_by(User.id)
        .limit(2)
    )
    user_ids = [int(user_id) for user_id in result.scalars().all()]
    if len(user_ids) == 1:
        return user_ids[0]
    logger.warning(
        "Agent identity unresolved; tenant_id=%s agent_name=%r matches=%d",
        tenant_id,
        agent_name,
        len(user_ids),
    )
    return None


__all__ = ["resolve_unique_agent_user_id"]
