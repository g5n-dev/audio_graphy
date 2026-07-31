"""Integration tests for the LLMCallLog (llm_call_logs) model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from audio_graphy.models.llm_call_log import LLMCallLog


@pytest.mark.integration
class TestLLMCallLogCRUD:
    """CRUD operations for the llm_call_logs table."""

    def test_create_llm_call_log(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="hash_001",
            tokens_in=100,
            tokens_out=50,
            cached=False,
            latency_ms=200,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        assert log.id is not None
        assert log.tokens_in == 100

    def test_read_llm_call_log(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-35b-a3b",
            prompt_hash="hash_002",
            tokens_in=200,
            tokens_out=100,
            cached=True,
            latency_ms=50,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        result = db_session.scalar(select(LLMCallLog).where(LLMCallLog.prompt_hash == "hash_002"))
        assert result is not None
        assert result.cached is True
        assert result.latency_ms == 50

    def test_update_llm_call_log(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="hash_003",
            tokens_in=10,
            tokens_out=5,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        log.latency_ms = 500
        db_session.commit()

        result = db_session.get(LLMCallLog, log.id)
        assert result is not None
        assert result.latency_ms == 500

    def test_delete_llm_call_log(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="hash_004",
            tokens_in=10,
            tokens_out=5,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()
        log_id = log.id

        db_session.delete(log)
        db_session.commit()

        assert db_session.get(LLMCallLog, log_id) is None


@pytest.mark.integration
class TestLLMCallLogConstraints:
    """Constraint validation for the llm_call_logs table."""

    def test_check_tokens_non_negative(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="neg_tok",
            tokens_in=-1,
            tokens_out=5,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_check_latency_non_negative(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="neg_lat",
            tokens_in=10,
            tokens_out=5,
            latency_ms=-100,
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_defaults(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            prompt_hash="defaults",
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        db_session.commit()

        assert log.tokens_in == 0
        assert log.tokens_out == 0
        assert log.cached is False
        assert log.latency_ms == 0
        assert log.event_kind == "logical_request"
        assert log.outcome == "success"
        assert log.attempt is None
        assert log.error_type is None
        assert log.model_tier == "other"
        assert log.requested_max_tokens is None
        assert log.cached_prefill_tokens == 0
        assert log.counterfactual_saved_input_tokens == 0
        assert log.counterfactual_saved_output_tokens == 0
        assert log.counterfactual_saved_tokens == 0
        assert log.cost_microunits == 0
        assert log.price_version is None
        assert log.finish_reason is None
        assert log.provider_request_id is None
        assert log.retry_class is None
        assert log.cache_lookup_reason is None
        assert log.cache_miss_reason is None
        assert log.unknown_billed is False

    def test_provider_attempt_must_be_positive(self, db_session: pytest.fixture) -> None:
        log = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            event_kind="provider_attempt",
            outcome="error",
            attempt=0,
            error_type="LLMRateLimitError",
            prompt_hash="invalid_attempt",
            logged_at=datetime.now(UTC),
        )
        db_session.add(log)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_provider_attempt_id_is_unique_per_tenant(
        self, db_session: pytest.fixture
    ) -> None:
        first = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            event_kind="provider_attempt",
            outcome="success",
            attempt=1,
            provider_attempt_id="provider-attempt-1",
            prompt_hash="attempt-1",
            logged_at=datetime.now(UTC),
        )
        duplicate = LLMCallLog(
            tenant_id="default",
            model="qwen3.6-27b",
            event_kind="provider_attempt",
            outcome="error",
            attempt=1,
            provider_attempt_id="provider-attempt-1",
            prompt_hash="attempt-1-duplicate",
            logged_at=datetime.now(UTC),
        )
        db_session.add(first)
        db_session.commit()
        db_session.add(duplicate)
        with pytest.raises((IntegrityError, OperationalError)):
            db_session.commit()
        db_session.rollback()

    def test_distinct_retry_attempt_ids_are_allowed(
        self, db_session: pytest.fixture
    ) -> None:
        db_session.add_all(
            [
                LLMCallLog(
                    tenant_id="default",
                    model="qwen3.6-27b",
                    event_kind="provider_attempt",
                    outcome="error",
                    attempt=1,
                    provider_attempt_id="retry-attempt-1",
                    prompt_hash="retry",
                    logged_at=datetime.now(UTC),
                ),
                LLMCallLog(
                    tenant_id="default",
                    model="qwen3.6-27b",
                    event_kind="provider_attempt",
                    outcome="success",
                    attempt=2,
                    provider_attempt_id="retry-attempt-2",
                    prompt_hash="retry",
                    logged_at=datetime.now(UTC),
                ),
            ]
        )
        db_session.commit()

        rows = db_session.scalars(
            select(LLMCallLog).where(LLMCallLog.prompt_hash == "retry")
        ).all()
        assert len(rows) == 2


@pytest.mark.integration
class TestLLMCallLogMultiTenant:
    """Multi-tenant isolation for the llm_call_logs table."""

    def test_tenant_isolation(self, db_session: pytest.fixture) -> None:
        log1 = LLMCallLog(
            tenant_id="ta",
            model="qwen3.6-27b",
            prompt_hash="ta_hash",
            logged_at=datetime.now(UTC),
        )
        log2 = LLMCallLog(
            tenant_id="tb",
            model="qwen3.6-27b",
            prompt_hash="tb_hash",
            logged_at=datetime.now(UTC),
        )
        db_session.add_all([log1, log2])
        db_session.commit()

        a = db_session.scalars(select(LLMCallLog).where(LLMCallLog.tenant_id == "ta")).all()
        b = db_session.scalars(select(LLMCallLog).where(LLMCallLog.tenant_id == "tb")).all()

        assert len(a) == 1
        assert len(b) == 1
