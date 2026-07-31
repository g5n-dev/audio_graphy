"""Query-budget and index contracts for governance control-plane hot paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.tag_governance import (
    TagEvaluationRun,
    TagGateResult,
    TagSchema,
)
from audio_graphy.services.tag_governance import TagGovernanceService


@pytest.fixture
async def query_factory() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], AsyncEngine]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False), engine
    await engine.dispose()


async def test_list_queries_are_bounded_and_evaluation_gates_are_batch_loaded(
    query_factory: tuple[async_sessionmaker[AsyncSession], AsyncEngine],
) -> None:
    factory, engine = query_factory
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add_all(
            [
                TagSchema(
                    tenant_id="chang_an",
                    key=f"schema-{index}",
                    name=f"标签体系 {index}",
                    status="draft",
                    created_by=1,
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(3)
            ]
        )
        runs = [
            TagEvaluationRun(
                tenant_id="chang_an",
                tagger_version_id=100 + index,
                baseline_tagger_version_id=200 + index,
                gold_set_version_id=300 + index,
                status="completed",
                metrics={},
                baseline_metrics={},
                passed=True,
                started_at=now,
                finished_at=now,
                created_by=1,
                created_at=now + timedelta(seconds=index),
            )
            for index in range(3)
        ]
        session.add_all(runs)
        await session.flush()
        session.add_all(
            [
                TagGateResult(
                    tenant_id="chang_an",
                    evaluation_run_id=run.id,
                    code="macro_f1",
                    passed=True,
                    actual=0.9,
                    threshold=0.8,
                    message="passed",
                )
                for run in runs
            ]
        )

    service = TagGovernanceService(factory)
    schemas = await service.list_schemas(tenant_id="chang_an", limit=2)
    assert [schema.key for schema in schemas] == ["schema-2", "schema-1"]

    selected_statements: list[str] = []

    def capture_select(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith("SELECT"):
            selected_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture_select)
    try:
        evaluations = await service.list_evaluations(tenant_id="chang_an", limit=2)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_select)

    assert len(evaluations) == 2
    assert all(len(gates) == 1 for _run, gates in evaluations)
    governance_selects = [
        statement
        for statement in selected_statements
        if "tag_evaluation_runs" in statement or "tag_gate_results" in statement
    ]
    assert len(governance_selects) == 2


def test_governance_hot_path_indexes_cover_filters_and_ordering() -> None:
    expected = {
        "tag_extraction_jobs": {
            "ix_tag_extraction_jobs_tenant_created": (
                "tenant_id",
                "created_at",
                "id",
            ),
        },
        "tag_extraction_runs": {
            "ix_tag_extraction_runs_deployment_terminal": (
                "tenant_id",
                "deployment_id",
                "status",
                "finished_at",
            ),
            "ix_tag_extraction_runs_tagger_terminal_subject": (
                "tenant_id",
                "tagger_version_id",
                "status",
                "finished_at",
                "subject_type",
                "subject_id",
            ),
        },
        "tag_assignment_facts": {
            "ix_tag_assignment_facts_extraction_run": (
                "tenant_id",
                "extraction_run_id",
            ),
            "ix_tag_assignment_facts_deployment_window": (
                "tenant_id",
                "deployment_id",
                "tagger_version_id",
                "tombstone",
                "assigned_at",
                "id",
            ),
        },
        "tag_review_decisions": {
            "ix_tag_review_decisions_window": (
                "tenant_id",
                "decided_at",
                "task_id",
                "id",
            ),
        },
        "tag_deployments": {
            "ix_tag_deployments_monitor": ("status", "tenant_id", "id"),
            "ix_tag_deployments_route": (
                "tenant_id",
                "status",
                "approved_at",
                "id",
            ),
            "ix_tag_deployments_baseline_active": (
                "tenant_id",
                "baseline_tagger_version_id",
                "status",
                "created_at",
                "id",
            ),
        },
        "tag_deployment_observations": {
            "ix_tag_deployment_observations_time": (
                "tenant_id",
                "deployment_id",
                "window_end",
                "id",
            ),
        },
        "tag_governance_audit_events": {
            "ix_tag_governance_audit_timeline": (
                "tenant_id",
                "occurred_at",
                "id",
            ),
        },
    }

    for table_name, expected_indexes in expected.items():
        table = Base.metadata.tables[table_name]
        actual = {
            index.name: tuple(column.name for column in index.columns) for index in table.indexes
        }
        for index_name, columns in expected_indexes.items():
            assert actual[index_name] == columns
