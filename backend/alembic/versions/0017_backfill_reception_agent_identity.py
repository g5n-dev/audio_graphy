"""Conservatively backfill stable reception agent identities.

Only an exact, tenant-local, role=agent name with exactly one match is
backfilled. Missing and duplicate names deliberately remain NULL so agent
authorization fails closed.

Revision ID: 0017_reception_agent_backfill
Revises: 0016_reception_agent_identity
Create Date: 2026-07-23 18:05:00.000000
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_reception_agent_backfill"
down_revision: str | None = "0016_reception_agent_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_AMBIGUITY_CORE_QUERY = """
SELECT
    r.id AS reception_id,
    r.tenant_id,
    r.agent_name,
    COUNT(u.id) AS matching_agent_users
FROM receptions AS r
LEFT JOIN users AS u
    ON u.tenant_id = r.tenant_id
   AND u.name = r.agent_name
   AND u.role = 'agent'
WHERE r.agent_name IS NOT NULL
  AND r.agent_user_id IS NULL
GROUP BY r.id, r.tenant_id, r.agent_name
HAVING COUNT(u.id) <> 1
"""
_AMBIGUITY_COUNT_QUERY = (
    "SELECT COUNT(*) FROM (" + _AMBIGUITY_CORE_QUERY + ") AS unresolved_receptions"  # noqa: S608 -- both fragments are static migration SQL
)
_AMBIGUITY_PREVIEW_QUERY = _AMBIGUITY_CORE_QUERY + " ORDER BY r.tenant_id, r.id LIMIT 100"


def upgrade() -> None:
    """Backfill only uniquely resolvable identities and log unresolved rows."""

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE receptions
            SET agent_user_id = (
                SELECT MIN(users.id)
                FROM users
                WHERE users.tenant_id = receptions.tenant_id
                  AND users.name = receptions.agent_name
                  AND users.role = 'agent'
            )
            WHERE agent_name IS NOT NULL
              AND agent_user_id IS NULL
              AND (
                  SELECT COUNT(users.id)
                  FROM users
                  WHERE users.tenant_id = receptions.tenant_id
                    AND users.name = receptions.agent_name
                    AND users.role = 'agent'
              ) = 1
            """
        )
    )

    unresolved_count_result = bind.execute(sa.text(_AMBIGUITY_COUNT_QUERY))
    unresolved_preview_result = bind.execute(sa.text(_AMBIGUITY_PREVIEW_QUERY))
    # Alembic's offline MockConnection emits SQL and returns ``None``.
    # Runtime diagnostics are available only during an online migration.
    if unresolved_count_result is None or unresolved_preview_result is None:
        return
    unresolved_count = int(unresolved_count_result.scalar_one())
    unresolved = list(unresolved_preview_result.mappings())
    if unresolved_count:
        preview = [
            {
                "reception_id": int(row["reception_id"]),
                "tenant_id": str(row["tenant_id"]),
                "agent_name": str(row["agent_name"]),
                "matching_agent_users": int(row["matching_agent_users"]),
            }
            for row in unresolved
        ]
        logger.warning(
            "Reception agent identity backfill left %d row(s) fail-closed; "
            "first %d unresolved rows: %s",
            unresolved_count,
            len(preview),
            preview,
        )


def downgrade() -> None:
    """Data backfill is intentionally not reversed."""
