"""Integration test for alembic migration roundtrip (upgrade head -> downgrade base).

Uses subprocess to invoke the alembic CLI, avoiding the local backend/alembic/
directory shadowing the installed alembic Python package.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
PYTHON_BIN = sys.executable

# Use the venv's alembic binary (not the system one) to avoid local
# backend/alembic/ directory shadowing the installed alembic package.
ALEMBIC_BIN = str(Path(PYTHON_BIN).parent / "alembic")

EXPECTED_TABLES = {
    "tenants",
    "users",
    "recordings",
    "segments",
    "chunks",
    "tag_facts",
    "tag_current",
    "tag_stats",
    "prompts",
    "vectors_entity",
    "vectors_chunk",
    "audit_logs",
    "llm_call_logs",
    "llm_cache_entries",
    "llm_cache_refs",
    "llm_cache_source_guards",
    "llm_cache_purges",
    # M6 WS-3
    "entity_aliases",
    # M6 WS-2
    "eval_runs",
    "recompute_tasks",
    # M7 WS-2
    "speaker_nodes",
    "speaker_links",
    "vectors_voiceprint",
    "vectors_audio",
    # M8
    "streaming_sessions",
    # M9
    "edge_events",
    "community_summaries",
    "leiden_jobs",
    "speaker_merge_pending",
    # Reception/dialogue workspace
    "receptions",
    "reception_recordings",
    "dialogue_units",
    "dialogue_state_transitions",
    "dialogue_tag_assignments",
    "provenance_events",
    "reception_automation_runs",
    # Versioned tag-governance closed loop
    "tag_schemas",
    "tag_schema_versions",
    "tagger_versions",
    "tag_extraction_jobs",
    "tag_extraction_runs",
    "tag_assignment_facts",
    "tag_assignment_current",
    "tag_review_tasks",
    "tag_review_decisions",
    "tag_gold_sets",
    "tag_gold_set_versions",
    "tag_gold_labels",
    "tag_evaluation_runs",
    "tag_evaluation_metrics",
    "tag_gate_results",
    "tag_deployments",
    "tag_deployment_observations",
    "tag_deployment_observation_samples",
    "tag_deployment_audit_subjects",
    "legacy_tag_mappings",
    "tag_governance_audit_events",
    # Semantic-tag Harness evolution
    "tag_harness_executions",
    "tag_harness_stage_traces",
    "tag_feedback_events",
    "tag_feedback_lane_assignments",
    "tag_evaluation_items",
    "tag_badcases",
    "tag_experience_cases",
    "tag_optimization_runs",
    "tag_optimization_trials",
}


def _run_alembic(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run alembic CLI command via subprocess using the installed binary."""
    cmd = [ALEMBIC_BIN, "-c", str(ALEMBIC_INI), *list(args)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
    )
    return result


@pytest.mark.integration
class TestAlembicRoundtrip:
    """Test alembic upgrade head and downgrade base roundtrip."""

    def test_0022_schema_trigger_allows_only_deprecation_lifecycle(
        self, mysql_container: Any
    ) -> None:
        """Published definitions stay immutable while the active pointer can advance."""

        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)
        database = f"alembic_0022_schema_{os.getpid()}"
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            conn.execute(text(f"CREATE DATABASE `{database}`"))

        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = database
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/{database}"
        )
        test_engine = create_engine(test_url)
        try:
            result = _run_alembic("upgrade", "head", env=env)
            assert result.returncode == 0, result.stderr
            with test_engine.begin() as conn:
                schema_result = conn.execute(
                    text(
                        """
                        INSERT INTO tag_schemas
                            (tenant_id, `key`, name, status, created_by)
                        VALUES
                            ('schema-tenant', 'lifecycle-schema',
                             'Lifecycle schema', 'published', 1)
                        """
                    )
                )
                schema_id = int(schema_result.lastrowid)
                version_ids: list[int] = []
                for version, checksum, status in (
                    ("1", "1" * 64, "published"),
                    ("2", "2" * 64, "draft"),
                ):
                    version_result = conn.execute(
                        text(
                            """
                            INSERT INTO tag_schema_versions
                                (tenant_id, schema_id, version, definitions,
                                 checksum, status, created_by)
                            VALUES
                                ('schema-tenant', :schema_id, :version,
                                 :definitions, :checksum, :status, 1)
                            """
                        ),
                        {
                            "schema_id": schema_id,
                            "version": version,
                            "definitions": "[]",
                            "checksum": checksum,
                            "status": status,
                        },
                    )
                    version_ids.append(int(version_result.lastrowid))

            with test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tag_schema_versions
                        SET status = 'deprecated'
                        WHERE id = :version_id
                        """
                    ),
                    {"version_id": version_ids[0]},
                )
                conn.execute(
                    text(
                        """
                        UPDATE tag_schema_versions
                        SET status = 'published', published_by = 1,
                            published_at = CURRENT_TIMESTAMP
                        WHERE id = :version_id
                        """
                    ),
                    {"version_id": version_ids[1]},
                )

            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tag_schema_versions
                        SET checksum = :checksum
                        WHERE id = :version_id
                        """
                    ),
                    {
                        "checksum": "3" * 64,
                        "version_id": version_ids[0],
                    },
                )

            with test_engine.connect() as conn:
                states = list(
                    conn.execute(
                        text(
                            """
                            SELECT version, status, checksum
                            FROM tag_schema_versions
                            WHERE schema_id = :schema_id
                            ORDER BY version
                            """
                        ),
                        {"schema_id": schema_id},
                    ).mappings()
                )
            assert [(row["version"], row["status"]) for row in states] == [
                ("1", "deprecated"),
                ("2", "published"),
            ]
            assert states[0]["checksum"] == "1" * 64
        finally:
            test_engine.dispose()
            with admin_engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            admin_engine.dispose()

    def test_0022_tagger_rollback_cannot_unlock_terminal_version(
        self, mysql_container: Any
    ) -> None:
        """A qualified Harness stays immutable after its legal rollback transition."""

        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)
        database = f"alembic_0022_tagger_terminal_{os.getpid()}"
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            conn.execute(text(f"CREATE DATABASE `{database}`"))

        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = database
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/{database}"
        )
        test_engine = create_engine(test_url)
        try:
            result = _run_alembic("upgrade", "head", env=env)
            assert result.returncode == 0, result.stderr
            with test_engine.begin() as conn:
                schema_result = conn.execute(
                    text(
                        """
                        INSERT INTO tag_schemas
                            (tenant_id, `key`, name, status, created_by)
                        VALUES
                            ('tagger-terminal-tenant', 'tagger-terminal-schema',
                             'Tagger terminal schema', 'published', 1)
                        """
                    )
                )
                schema_id = int(schema_result.lastrowid)
                version_result = conn.execute(
                    text(
                        """
                        INSERT INTO tag_schema_versions
                            (tenant_id, schema_id, version, definitions,
                             checksum, status, created_by, published_by,
                             published_at)
                        VALUES
                            ('tagger-terminal-tenant', :schema_id, '1',
                             :definitions, :checksum, 'published', 1, 1,
                             CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "schema_id": schema_id,
                        "definitions": "[]",
                        "checksum": "s" * 64,
                    },
                )
                schema_version_id = int(version_result.lastrowid)
                tagger_result = conn.execute(
                    text(
                        """
                        INSERT INTO tagger_versions
                            (tenant_id, schema_version_id, version, engine,
                             prompt_content, rule_bundle, model_version,
                             thresholds, config_checksum, status, created_by,
                             qualified_at)
                        VALUES
                            ('tagger-terminal-tenant', :schema_version_id, '1',
                             'hybrid', 'immutable prompt', :rule_bundle,
                             'test-model', :thresholds, :checksum, 'qualified',
                             1, CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "schema_version_id": schema_version_id,
                        "rule_bundle": '{"rules":[]}',
                        "thresholds": '{"default":0.7}',
                        "checksum": "t" * 64,
                    },
                )
                tagger_id = int(tagger_result.lastrowid)

            with test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tagger_versions
                        SET status = 'rejected', qualified_at = NULL
                        WHERE id = :tagger_id
                        """
                    ),
                    {"tagger_id": tagger_id},
                )

            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tagger_versions
                        SET prompt_content = 'mutated after rollback'
                        WHERE id = :tagger_id
                        """
                    ),
                    {"tagger_id": tagger_id},
                )

            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tagger_versions
                        SET status = 'draft'
                        WHERE id = :tagger_id
                        """
                    ),
                    {"tagger_id": tagger_id},
                )

            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM tagger_versions WHERE id = :tagger_id"),
                    {"tagger_id": tagger_id},
                )

            with test_engine.connect() as conn:
                terminal = (
                    conn.execute(
                        text(
                            """
                        SELECT status, prompt_content, qualified_at
                        FROM tagger_versions
                        WHERE id = :tagger_id
                        """
                        ),
                        {"tagger_id": tagger_id},
                    )
                    .mappings()
                    .one()
                )
            assert terminal["status"] == "rejected"
            assert terminal["prompt_content"] == "immutable prompt"
            assert terminal["qualified_at"] is None
        finally:
            test_engine.dispose()
            with admin_engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            admin_engine.dispose()

    def test_0022_preserves_legacy_tagger_checksum_identity(self, mysql_container: Any) -> None:
        """Equivalent legacy rows must not collide while Harness fields are backfilled."""

        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)
        database = f"alembic_0022_checksum_{os.getpid()}"
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            conn.execute(text(f"CREATE DATABASE `{database}`"))

        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = database
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/{database}"
        )
        test_engine = create_engine(test_url)
        try:
            result = _run_alembic("upgrade", "0021_llm_cache", env=env)
            assert result.returncode == 0, result.stderr

            with test_engine.begin() as conn:
                schema_result = conn.execute(
                    text(
                        """
                        INSERT INTO tag_schemas
                            (tenant_id, `key`, name, status, created_by)
                        VALUES
                            ('checksum-tenant', 'checksum-schema',
                             'Checksum schema', 'draft', 1)
                        """
                    )
                )
                schema_id = int(schema_result.lastrowid)
                version_result = conn.execute(
                    text(
                        """
                        INSERT INTO tag_schema_versions
                            (tenant_id, schema_id, version, definitions,
                             checksum, status, created_by)
                        VALUES
                            ('checksum-tenant', :schema_id, '1',
                             :definitions, :checksum, 'draft', 1)
                        """
                    ),
                    {
                        "schema_id": schema_id,
                        "definitions": "[]",
                        "checksum": "c" * 64,
                    },
                )
                schema_version_id = int(version_result.lastrowid)
                for version, checksum in (
                    ("legacy-a", "a" * 64),
                    ("legacy-b", "b" * 64),
                ):
                    conn.execute(
                        text(
                            """
                            INSERT INTO tagger_versions
                                (tenant_id, schema_version_id, version, engine,
                                 prompt_content, rule_bundle, model_version,
                                 thresholds, config_checksum, status, created_by)
                            VALUES
                                ('checksum-tenant', :schema_version_id, :version,
                                 'hybrid', 'same legacy prompt', :rule_bundle,
                                 'same-model', :thresholds, :checksum, 'draft', 1)
                            """
                        ),
                        {
                            "schema_version_id": schema_version_id,
                            "version": version,
                            "rule_bundle": '{"rules":[]}',
                            "thresholds": '{"default":0.7}',
                            "checksum": checksum,
                        },
                    )

            result = _run_alembic("upgrade", "head", env=env)
            assert result.returncode == 0, (
                "0022 must preserve opaque legacy checksum identity:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            with test_engine.connect() as conn:
                upgraded = conn.execute(
                    text(
                        """
                        SELECT version, config_checksum, harness_spec
                        FROM tagger_versions
                        WHERE tenant_id = 'checksum-tenant'
                        ORDER BY version
                        """
                    )
                ).mappings()
                rows = list(upgraded)
            assert [row["config_checksum"] for row in rows] == [
                "a" * 64,
                "b" * 64,
            ]
            assert all(row["harness_spec"] is not None for row in rows)

            with test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO tag_extraction_jobs
                            (tenant_id, job_type, status, scope,
                             idempotency_key, failed_subset, created_by)
                        VALUES
                            ('checksum-tenant', 'optimize', 'queued', '{}',
                             'downgrade-preflight', '[]', 1)
                        """
                    )
                )
            blocked = _run_alembic("downgrade", "0021_llm_cache", env=env)
            assert blocked.returncode != 0
            assert "incompatible 0022 data" in f"{blocked.stdout}\n{blocked.stderr}"
            with test_engine.connect() as conn:
                tables_after_block = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
            assert "tag_optimization_runs" in tables_after_block

            with test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        DELETE FROM tag_extraction_jobs
                        WHERE tenant_id = 'checksum-tenant'
                          AND idempotency_key = 'downgrade-preflight'
                        """
                    )
                )
            result = _run_alembic("downgrade", "0021_llm_cache", env=env)
            assert result.returncode == 0, (
                "0022 downgrade must not collapse opaque checksums:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            with test_engine.connect() as conn:
                downgraded = list(
                    conn.execute(
                        text(
                            """
                            SELECT version, config_checksum
                            FROM tagger_versions
                            WHERE tenant_id = 'checksum-tenant'
                            ORDER BY version
                            """
                        )
                    ).mappings()
                )
            assert [row["config_checksum"] for row in downgraded] == [
                "a" * 64,
                "b" * 64,
            ]
        finally:
            test_engine.dispose()
            with admin_engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            admin_engine.dispose()

    def test_0023_backfills_and_isolates_t3_feedback_lanes(
        self,
        mysql_container: Any,
    ) -> None:
        """Existing T3 rows become visible only in their server-frozen lane."""

        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)
        database = f"alembic_0023_feedback_{os.getpid()}"
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            conn.execute(text(f"CREATE DATABASE `{database}`"))

        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = database
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/{database}"
        )
        test_engine = create_engine(test_url)
        try:
            result = _run_alembic("upgrade", "0022_tag_harness_evolution", env=env)
            assert result.returncode == 0, result.stderr
            with test_engine.begin() as conn:
                schema_id = int(
                    conn.execute(
                        text(
                            """
                            INSERT INTO tag_schemas
                                (tenant_id, `key`, name, status, created_by)
                            VALUES
                                ('lane-tenant', 'lane-schema',
                                 'Lane schema', 'draft', 1)
                            """
                        )
                    ).lastrowid
                )
                schema_version_id = int(
                    conn.execute(
                        text(
                            """
                            INSERT INTO tag_schema_versions
                                (tenant_id, schema_id, version, definitions,
                                 checksum, status, created_by)
                            VALUES
                                ('lane-tenant', :schema_id, '1', '[]',
                                 :checksum, 'draft', 1)
                            """
                        ),
                        {"schema_id": schema_id, "checksum": "1" * 64},
                    ).lastrowid
                )
                gold_set_id = int(
                    conn.execute(
                        text(
                            """
                            INSERT INTO tag_gold_sets
                                (tenant_id, `key`, name, schema_version_id, created_by)
                            VALUES
                                ('lane-tenant', 'lane-gold', 'Lane gold',
                                 :schema_version_id, 1)
                            """
                        ),
                        {"schema_version_id": schema_version_id},
                    ).lastrowid
                )
                gold_version_id = int(
                    conn.execute(
                        text(
                            """
                            INSERT INTO tag_gold_set_versions
                                (tenant_id, gold_set_id, version, status,
                                 checksum, item_count)
                            VALUES
                                ('lane-tenant', :gold_set_id, '1', 'draft',
                                 :checksum, 2)
                            """
                        ),
                        {"gold_set_id": gold_set_id, "checksum": "2" * 64},
                    ).lastrowid
                )

                seeded: dict[str, dict[str, int]] = {}
                for index, split in enumerate(("validation", "holdout", None), start=1):
                    task_id = int(
                        conn.execute(
                            text(
                                """
                                INSERT INTO tag_review_tasks
                                    (tenant_id, batch_id, subject_type, subject_id,
                                     tag_key, evidence_refs, reason, status,
                                     priority, created_by)
                                VALUES
                                    ('lane-tenant', :batch_id, 'dialogue_unit',
                                     :subject_id, 'intent', '[]', 'gold',
                                     'resolved', 0, 1)
                                """
                            ),
                            {
                                "batch_id": f"lane-{index}",
                                "subject_id": index,
                            },
                        ).lastrowid
                    )
                    decision_id = int(
                        conn.execute(
                            text(
                                """
                                INSERT INTO tag_review_decisions
                                    (tenant_id, task_id, action, corrected_value,
                                     reason_code, evidence_refs, reviewer_user_id,
                                     adjudication, decided_at, truth_state,
                                     truth_tier, annotator_round,
                                     primary_failure_stage)
                                VALUES
                                    ('lane-tenant', :task_id, 'correct',
                                     :corrected_value, 'backfill', '[]', 9,
                                     1, CURRENT_TIMESTAMP, 'present', 't3', 3,
                                     'tag_reasoning')
                                """
                            ),
                            {
                                "task_id": task_id,
                                "corrected_value": json.dumps("purchase"),
                            },
                        ).lastrowid
                    )
                    feedback_id = int(
                        conn.execute(
                            text(
                                """
                                INSERT INTO tag_feedback_events
                                    (tenant_id, review_decision_id, source,
                                     truth_tier, subject_type, subject_id,
                                     tag_key, truth_state, error_stage,
                                     correction, payload, training_eligible,
                                     occurred_at)
                                VALUES
                                    ('lane-tenant', :decision_id, 'human', 't3',
                                     'dialogue_unit', :subject_id, 'intent',
                                     'present', 'tag_reasoning', :correction,
                                     '{}', 1, CURRENT_TIMESTAMP)
                                """
                            ),
                            {
                                "decision_id": decision_id,
                                "subject_id": index,
                                "correction": json.dumps(
                                    {
                                        "action": "correct",
                                        "corrected_value": "purchase",
                                    }
                                ),
                            },
                        ).lastrowid
                    )
                    gold_label_id = 0
                    if split is not None:
                        gold_label_id = int(
                            conn.execute(
                                text(
                                    """
                                    INSERT INTO tag_gold_labels
                                        (tenant_id, gold_set_version_id,
                                         review_decision_id, subject_type,
                                         subject_id, tag_key, tag_value,
                                         evidence_refs, split, truth_state,
                                         truth_tier)
                                    VALUES
                                        ('lane-tenant', :gold_version_id,
                                         :decision_id, 'dialogue_unit',
                                         :subject_id, 'intent', :tag_value,
                                         '[]', :split, 'present', 't3')
                                    """
                                ),
                                {
                                    "gold_version_id": gold_version_id,
                                    "decision_id": decision_id,
                                    "subject_id": index,
                                    "tag_value": json.dumps("purchase"),
                                    "split": split,
                                },
                            ).lastrowid
                        )
                    badcase_id = int(
                        conn.execute(
                            text(
                                """
                                INSERT INTO tag_badcases
                                    (tenant_id, source_feedback_event_id,
                                     subject_type, subject_id, tag_key,
                                     failure_stage, failure_mode, signature_hash,
                                     cluster_key, root_cause, status,
                                     regression_result, occurrence_count,
                                     first_seen_at, last_seen_at)
                                VALUES
                                    ('lane-tenant', :feedback_id,
                                     'dialogue_unit', :subject_id, 'intent',
                                     'tag_reasoning', 'correct:backfill',
                                     :signature_hash, 'tag_reasoning:intent',
                                     :root_cause, 'open', '{}', 1,
                                     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                """
                            ),
                            {
                                "feedback_id": feedback_id,
                                "subject_id": index,
                                "signature_hash": str(index) * 64,
                                "root_cause": json.dumps({"latest_feedback_event_id": feedback_id}),
                            },
                        ).lastrowid
                    )
                    experience_id = int(
                        conn.execute(
                            text(
                                """
                                INSERT INTO tag_experience_cases
                                    (tenant_id, source_badcase_id,
                                     source_feedback_event_id, scene_signature,
                                     failure_signature, harness_spec,
                                     reward_vector, outcome, quality_tier,
                                     eligible, checksum, materialized_at)
                                VALUES
                                    ('lane-tenant', :badcase_id, :feedback_id,
                                     '{}', '{}', '{}', '{}', 'successful',
                                     't3', 1, :checksum, CURRENT_TIMESTAMP)
                                """
                            ),
                            {
                                "badcase_id": badcase_id,
                                "feedback_id": feedback_id,
                                "checksum": chr(96 + index) * 64,
                            },
                        ).lastrowid
                    )
                    seeded[split or "pending"] = {
                        "feedback_id": feedback_id,
                        "gold_label_id": gold_label_id,
                        "badcase_id": badcase_id,
                        "experience_id": experience_id,
                    }

            result = _run_alembic("upgrade", "head", env=env)
            assert result.returncode == 0, (
                "0023 feedback-lane backfill failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            with test_engine.connect() as conn:
                lanes = {
                    int(row["feedback_event_id"]): str(row["split"])
                    for row in conn.execute(
                        text(
                            """
                            SELECT feedback_event_id, split
                            FROM tag_feedback_lane_assignments
                            WHERE tenant_id = 'lane-tenant'
                            """
                        )
                    ).mappings()
                }
                badcases = {
                    int(row["id"]): row
                    for row in conn.execute(
                        text(
                            """
                            SELECT id, dataset_split, status
                            FROM tag_badcases
                            WHERE tenant_id = 'lane-tenant'
                            """
                        )
                    ).mappings()
                }
                experiences = {
                    int(row["id"]): row
                    for row in conn.execute(
                        text(
                            """
                            SELECT id, dataset_split, eligible
                            FROM tag_experience_cases
                            WHERE tenant_id = 'lane-tenant'
                            """
                        )
                    ).mappings()
                }

            assert lanes == {
                seeded["validation"]["feedback_id"]: "validation",
                seeded["holdout"]["feedback_id"]: "holdout",
            }
            for split in ("validation", "holdout", "pending"):
                badcase = badcases[seeded[split]["badcase_id"]]
                experience = experiences[seeded[split]["experience_id"]]
                assert badcase["dataset_split"] == split
                assert experience["dataset_split"] == split
                assert bool(experience["eligible"]) is (split == "validation")
                assert badcase["status"] == ("open" if split == "validation" else "ignored")

            lane_id = seeded["holdout"]["feedback_id"]
            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE tag_feedback_lane_assignments
                        SET split = 'validation'
                        WHERE feedback_event_id = :feedback_event_id
                        """
                    ),
                    {"feedback_event_id": lane_id},
                )
            with pytest.raises(DBAPIError), test_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        DELETE FROM tag_feedback_lane_assignments
                        WHERE feedback_event_id = :feedback_event_id
                        """
                    ),
                    {"feedback_event_id": lane_id},
                )
            result = _run_alembic(
                "downgrade",
                "0022_tag_harness_evolution",
                env=env,
            )
            assert result.returncode == 0, (
                "0023 feedback-lane downgrade failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            with test_engine.connect() as conn:
                tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
                badcase_columns = {
                    str(row["Field"])
                    for row in conn.execute(text("SHOW COLUMNS FROM tag_badcases")).mappings()
                }
                experience_columns = {
                    str(row["Field"])
                    for row in conn.execute(
                        text("SHOW COLUMNS FROM tag_experience_cases")
                    ).mappings()
                }
            assert "tag_feedback_lane_assignments" not in tables
            assert "dataset_split" not in badcase_columns
            assert "dataset_split" not in experience_columns
        finally:
            test_engine.dispose()
            with admin_engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
            admin_engine.dispose()

    def test_alembic_upgrade_creates_all_tables(
        self, mysql_container: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify alembic upgrade head creates every registered table."""
        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)

        # Create a fresh database for alembic testing
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS alembic_rt_test"))
            conn.execute(text("CREATE DATABASE alembic_rt_test"))
            conn.commit()
        admin_engine.dispose()

        # Set env vars for alembic subprocess
        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = "alembic_rt_test"

        # Clear settings cache in this process too (for any later use)
        from audio_graphy.config import get_settings

        get_settings.cache_clear()

        # Run alembic upgrade head
        result = _run_alembic("upgrade", "head", env=env)
        assert result.returncode == 0, (
            f"alembic upgrade head failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Verify all 13 tables exist
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/alembic_rt_test"
        )
        test_engine = create_engine(test_url)
        governance_indexes = {
            "tag_extraction_jobs": {
                "ix_tag_extraction_jobs_tenant_created": [
                    "tenant_id",
                    "created_at",
                    "id",
                ],
            },
            "tag_extraction_runs": {
                "ix_tag_extraction_runs_deployment_terminal": [
                    "tenant_id",
                    "deployment_id",
                    "status",
                    "finished_at",
                ],
            },
            "tag_assignment_facts": {
                "ix_tag_assignment_facts_deployment_window": [
                    "tenant_id",
                    "deployment_id",
                    "tagger_version_id",
                    "tombstone",
                    "assigned_at",
                    "id",
                ],
            },
            "tag_deployments": {
                "ix_tag_deployments_monitor": ["status", "tenant_id", "id"],
            },
            "tag_deployment_observations": {
                "ix_tag_deployment_observations_time": [
                    "tenant_id",
                    "deployment_id",
                    "window_end",
                    "id",
                ],
            },
            "tag_optimization_runs": {
                "ux_tag_optimization_runs_sealed_release": [
                    "tenant_id",
                    "sealed_release_key",
                ],
            },
        }
        evolution_columns = {
            "tagger_versions": {
                "harness_spec_version",
                "harness_spec",
                "parent_version_id",
                "origin",
                "optimization_run_id",
                "change_summary",
            },
            "tag_review_tasks": {
                "review_bundle_id",
                "selection_policy",
                "selection_policy_version",
                "sampling_probability",
                "blind_mode",
                "source_deployment_id",
                "source_extraction_run_id",
                "source_harness_execution_id",
            },
            "tag_review_decisions": {
                "truth_state",
                "truth_tier",
                "annotator_round",
                "primary_failure_stage",
                "reason_codes",
                "reviewer_confidence",
                "review_duration_ms",
            },
            "tag_gold_set_versions": {
                "dataset_snapshot_hash",
                "completeness_manifest",
            },
            "tag_gold_labels": {
                "truth_state",
                "truth_tier",
                "input_hash",
                "input_snapshot",
                "annotation_quality",
                "cohort",
                "completeness_manifest",
            },
            "tag_evaluation_runs": {
                "evaluator_version",
                "dataset_snapshot_hash",
            },
            "tag_optimization_runs": {
                "job_id",
                "baseline_tagger_version_id",
                "gold_set_version_id",
                "dataset_snapshot_hash",
                "sealed_release_key",
            },
            "tag_deployment_observations": {
                "source",
                "provenance",
                "is_trusted",
                "served_count",
                "paired_count",
                "audited_count",
                "adjudicated_count",
            },
            "tag_feedback_lane_assignments": {
                "feedback_event_id",
                "source_gold_label_id",
                "gold_set_version_id",
                "split",
                "assigned_by",
                "assigned_at",
            },
            "tag_badcases": {"dataset_split"},
            "tag_experience_cases": {"dataset_split"},
        }
        with test_engine.connect() as conn:
            result_set = conn.execute(text("SHOW TABLES"))
            tables = {row[0] for row in result_set}
            trigger_names = {
                str(row["Trigger"]) for row in conn.execute(text("SHOW TRIGGERS")).mappings()
            }
            llm_call_log_columns = {
                str(row["Field"])
                for row in conn.execute(text("SHOW COLUMNS FROM llm_call_logs")).mappings()
            }
            llm_attempt_index_rows = (
                conn.execute(
                    text(
                        "SHOW INDEX FROM llm_call_logs "
                        "WHERE Key_name = 'ux_llm_call_logs_provider_attempt'"
                    )
                )
                .mappings()
                .all()
            )
            voiceprint_index_rows = (
                conn.execute(
                    text(
                        "SHOW INDEX FROM vectors_voiceprint "
                        "WHERE Key_name = 'ix_vp_tenant_speaker_created'"
                    )
                )
                .mappings()
                .all()
            )
            migrated_index_columns: dict[tuple[str, str], list[str]] = {}
            for table_name, indexes in governance_indexes.items():
                for index_name in indexes:
                    rows = (
                        conn.execute(
                            text(f"SHOW INDEX FROM `{table_name}` WHERE Key_name = '{index_name}'")
                        )
                        .mappings()
                        .all()
                    )
                    migrated_index_columns[(table_name, index_name)] = [
                        str(row["Column_name"])
                        for row in sorted(rows, key=lambda row: row["Seq_in_index"])
                    ]
            migrated_columns = {
                table_name: {
                    str(row["Field"])
                    for row in conn.execute(text(f"SHOW COLUMNS FROM `{table_name}`"))
                    .mappings()
                    .all()
                }
                for table_name in evolution_columns
            }

        assert EXPECTED_TABLES.issubset(tables), f"Missing tables: {EXPECTED_TABLES - tables}"
        assert "_alembic_sentinel" not in tables
        assert {
            "trg_tag_assignment_facts_no_update",
            "trg_tag_assignment_facts_no_delete",
            "trg_tag_review_decisions_no_update",
            "trg_tag_review_decisions_no_delete",
            "trg_tag_harness_stage_traces_no_update",
            "trg_tag_harness_stage_traces_no_delete",
            "trg_tag_harness_executions_no_update",
            "trg_tag_harness_executions_no_delete",
            "trg_tag_feedback_events_no_update",
            "trg_tag_feedback_events_no_delete",
            "trg_tag_feedback_lane_assignments_no_update",
            "trg_tag_feedback_lane_assignments_no_delete",
            "trg_tag_schema_versions_terminal_no_update",
            "trg_tag_schema_versions_terminal_no_delete",
            "trg_tagger_versions_terminal_no_update",
            "trg_tagger_versions_terminal_no_delete",
            "trg_tag_gold_set_versions_terminal_no_update",
            "trg_tag_gold_set_versions_terminal_no_delete",
            "trg_tag_evaluation_runs_certified_no_update",
            "trg_tag_evaluation_runs_certified_no_delete",
            "trg_tag_gold_labels_terminal_no_insert",
            "trg_tag_gold_labels_terminal_no_update",
            "trg_tag_gold_labels_terminal_no_delete",
            "trg_tag_evaluation_metrics_terminal_no_insert",
            "trg_tag_evaluation_metrics_terminal_no_update",
            "trg_tag_evaluation_metrics_terminal_no_delete",
            "trg_tag_gate_results_terminal_no_insert",
            "trg_tag_gate_results_terminal_no_update",
            "trg_tag_gate_results_terminal_no_delete",
            "trg_tag_evaluation_items_terminal_no_insert",
            "trg_tag_evaluation_items_terminal_no_update",
            "trg_tag_evaluation_items_terminal_no_delete",
        } <= trigger_names
        assert {
            "event_kind",
            "outcome",
            "attempt",
            "error_type",
            "purpose",
            "cache_source",
            "provider_called",
            "logical_request_id",
            "provider_attempt_id",
            "model_tier",
            "requested_max_tokens",
            "tagger_version_id",
            "deployment_id",
            "evaluation_run_id",
            "optimization_run_id",
            "optimization_trial_id",
            "cached_prefill_tokens",
            "counterfactual_saved_input_tokens",
            "counterfactual_saved_output_tokens",
            "counterfactual_saved_tokens",
            "cost_microunits",
            "price_version",
            "finish_reason",
            "provider_request_id",
            "retry_class",
            "cache_lookup_reason",
            "cache_miss_reason",
            "unknown_billed",
        } <= llm_call_log_columns
        assert [
            row["Column_name"]
            for row in sorted(llm_attempt_index_rows, key=lambda row: row["Seq_in_index"])
        ] == ["tenant_id", "provider_attempt_id"]
        assert {int(row["Non_unique"]) for row in llm_attempt_index_rows} == {0}
        assert [
            row["Column_name"]
            for row in sorted(voiceprint_index_rows, key=lambda row: row["Seq_in_index"])
        ] == ["tenant_id", "speaker_entity_id", "created_at"]
        for table_name, indexes in governance_indexes.items():
            for index_name, expected_columns in indexes.items():
                assert migrated_index_columns[(table_name, index_name)] == expected_columns
        for table_name, expected_columns in evolution_columns.items():
            assert expected_columns.issubset(migrated_columns[table_name])

        # Run alembic downgrade base
        result = _run_alembic("downgrade", "base", env=env)
        assert result.returncode == 0, (
            f"alembic downgrade base failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Verify all 13 tables are dropped
        with test_engine.connect() as conn:
            result_set = conn.execute(text("SHOW TABLES"))
            tables = {row[0] for row in result_set}

        assert not EXPECTED_TABLES.intersection(tables), (
            f"Tables not dropped: {EXPECTED_TABLES.intersection(tables)}"
        )

        test_engine.dispose()

        # Cleanup
        get_settings.cache_clear()
