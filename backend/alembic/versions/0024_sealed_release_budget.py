"""Reserve one sealed-holdout release budget per frozen dataset snapshot.

Revision ID: 0024_sealed_release_budget
Revises: 0023_feedback_lane_isolation
Create Date: 2026-07-25 23:30:00.000000
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0024_sealed_release_budget"
down_revision: str | None = "0023_feedback_lane_isolation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _canonical_release_key(dataset_snapshot_hash: str) -> str:
    payload = json.dumps(
        {"dataset_snapshot_hash": dataset_snapshot_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _backfill_existing_release_reservations() -> None:
    connection = op.get_bind()
    runs = sa.table(
        "tag_optimization_runs",
        sa.column("id", sa.BigInteger()),
        sa.column("tenant_id", sa.String()),
        sa.column("dataset_snapshot_hash", sa.String()),
        sa.column("sealed_release_key", sa.String()),
        sa.column("job_id", sa.BigInteger()),
        sa.column("status", sa.String()),
        sa.column("phase", sa.String()),
        sa.column("summary", sa.JSON()),
    )
    audits = sa.table(
        "tag_governance_audit_events",
        sa.column("tenant_id", sa.String()),
        sa.column("resource_type", sa.String()),
        sa.column("resource_id", sa.BigInteger()),
        sa.column("action", sa.String()),
        sa.column("actor_user_id", sa.BigInteger()),
        sa.column("payload", sa.JSON()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
    )
    candidates_by_snapshot: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in connection.execute(
        sa.select(
            runs.c.id,
            runs.c.tenant_id,
            runs.c.dataset_snapshot_hash,
            runs.c.job_id,
            runs.c.status,
            runs.c.phase,
            runs.c.summary,
        ).order_by(runs.c.tenant_id, runs.c.dataset_snapshot_hash, runs.c.id)
    ).mappings():
        summary = _summary_mapping(row["summary"])
        used = int(summary.get("sealed_holdout_queries_used") or 0)
        is_release_run = (
            row["job_id"] is not None
            or str(row["status"]) in {"queued", "running"}
            or str(row["phase"]) == "holdout"
            or used >= 1
        )
        snapshot_hash = str(row["dataset_snapshot_hash"] or "")
        if not is_release_run or not snapshot_hash:
            continue
        normalized = dict(row)
        normalized["_sealed_used"] = used
        candidates_by_snapshot.setdefault(
            (str(row["tenant_id"]), snapshot_hash),
            [],
        ).append(normalized)

    now = datetime.now(UTC)
    for (tenant_id, snapshot_hash), candidates in candidates_by_snapshot.items():
        ordered = sorted(
            candidates,
            key=lambda row: (
                -int(int(row["_sealed_used"]) >= 1),
                -int(str(row["phase"]) == "holdout"),
                -int(str(row["status"]) in {"queued", "running"}),
                int(row["id"]),
            ),
        )
        reservation = ordered[0]
        release_key = _canonical_release_key(snapshot_hash)
        connection.execute(
            runs.update()
            .where(runs.c.id == int(reservation["id"]))
            .values(sealed_release_key=release_key)
        )
        duplicate_ids = [int(row["id"]) for row in ordered[1:]]
        if duplicate_ids:
            connection.execute(
                audits.insert().values(
                    tenant_id=tenant_id,
                    resource_type="tag_optimization_run",
                    resource_id=int(reservation["id"]),
                    action="sealed_release_budget_migration_deduplicated",
                    actor_user_id=0,
                    payload={
                        "dataset_snapshot_hash": snapshot_hash,
                        "reservation_run_id": int(reservation["id"]),
                        "duplicate_legacy_run_ids": duplicate_ids,
                    },
                    occurred_at=now,
                )
            )


def upgrade() -> None:
    op.add_column(
        "tag_optimization_runs",
        sa.Column("sealed_release_key", sa.String(length=64), nullable=True),
    )
    _backfill_existing_release_reservations()
    op.create_index(
        "ux_tag_optimization_runs_sealed_release",
        "tag_optimization_runs",
        ["tenant_id", "sealed_release_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_tag_optimization_runs_sealed_release",
        table_name="tag_optimization_runs",
    )
    op.drop_column("tag_optimization_runs", "sealed_release_key")
