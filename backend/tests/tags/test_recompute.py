"""Unit tests for RecomputeService — prompt version switch recomputation.

Tests: create_task, dry_run, execute_task, get_task_status.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.errors import TaskNotFoundError
from audio_graphy.services.legacy_tag_compatibility import CanonicalLegacyTarget
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.tags.recompute import (
    PromptDryRunBudgetError,
    RecomputeService,
    stratified_dialogue_unit_sample,
)

TENANT = "chang_an"


@pytest.mark.asyncio
class TestRecomputeService:
    """Tests for RecomputeService."""

    async def test_create_task(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """create_task creates a RecomputeTask row with correct total."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=["quality.greeting"],
            recording_ids=None,
        )
        assert task.task_id is not None
        assert task.task_id.startswith("recompute-")
        assert task.status == "pending"
        assert task.prompt_version == "v2"
        assert task.total >= 1  # The seeded recording has prompt_version="v1"

    async def test_create_task_with_recording_filter(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """create_task respects recording_ids filter."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=None,
            recording_ids=[999],  # Non-existent recording
        )
        assert task.total == 0  # No recordings match the filter

    async def test_dry_run(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """dry_run returns a diff dict without writing."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        result = await svc.dry_run(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=["quality.greeting", "quality.closing"],
            recording_ids=None,
        )
        assert result["dry_run"] is True
        assert "affected_count" in result
        assert "changed_count" in result
        assert "unchanged_count" in result
        assert "changes_preview" in result
        assert result["affected_count"] >= 1

    async def test_dry_run_no_recordings(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        file_index: FileIndex,
    ) -> None:
        """dry_run returns zero counts when no recordings are affected."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        result = await svc.dry_run(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=None,
            recording_ids=None,
        )
        assert result["affected_count"] == 0
        assert result["changed_count"] == 0

    async def test_dry_run_with_unchanged_prompt_makes_zero_provider_calls(
        self,
        session_factory,
        mock_bundle,
        file_index,
        seeded_recording,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A version-only change must not破坏缓存并重发相同 prompt."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)

        async def unexpected_call(*args, **kwargs):
            del args, kwargs
            raise AssertionError("unchanged prompt must not call the tagger")

        monkeypatch.setattr(svc, "_compute_tag_batch", unexpected_call)
        result = await svc.dry_run(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=["quality.greeting"],
            recording_ids=None,
            prompt_content="同一条规则\r\n",
            baseline_prompt_content="同一条规则\n",
        )

        assert result["affected_count"] >= 1
        assert result["sampled_count"] == 0
        assert result["estimated_tokens"] == 0
        assert result["provider_calls"] == 0

    async def test_canonical_prompt_dry_run_reuses_unchanged_recipe_without_calls(
        self,
        session_factory,
        mock_bundle,
        file_index,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prompt 内容未变时，直接绑定基线候选，且不触碰 Provider。"""

        class UnexpectedExtractor:
            async def predict_dialogue_unit(self, **kwargs):
                del kwargs
                raise AssertionError("unchanged canonical prompt must not call Provider")

        svc = RecomputeService(
            session_factory,
            mock_bundle,
            file_index,
            tag_extractor=UnexpectedExtractor(),
        )

        async def load_baseline(*args, **kwargs):
            del args, kwargs
            return SimpleNamespace(id=31, prompt_content="同一条规则\n")

        async def unexpected_materialization(*args, **kwargs):
            del args, kwargs
            raise AssertionError("unchanged prompt must not create another TaggerVersion")

        monkeypatch.setattr(svc, "_load_tagger", load_baseline)
        monkeypatch.setattr(
            svc,
            "_materialize_prompt_candidate",
            unexpected_materialization,
        )
        result = await svc.dry_run_prompt_candidate(
            tenant_id=TENANT,
            prompt_id=7,
            prompt_version="v2",
            prompt_content="同一条规则\r\n",
            resolved_target=CanonicalLegacyTarget(
                dialogue_unit_ids=(11, 12),
                tag_keys=("intent",),
                tagger_version_id=31,
            ),
            actor_user_id=1,
            sample_limit=100,
            max_provider_tokens=1,
            max_provider_calls=1,
        )

        assert result["candidate_tagger_version_id"] == 31
        assert result["affected_count"] == 2
        assert result["sampled_count"] == 0
        assert result["estimated_tokens"] == 0
        assert result["estimated_provider_calls"] == 0
        assert result["provider_calls"] == 0

    async def test_canonical_prompt_dry_run_enforces_budget_before_provider(
        self,
        session_factory,
        mock_bundle,
        file_index,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """整个样本预算必须在第一次 Provider 调用前完成原子式预检。"""

        class UnexpectedExtractor:
            async def predict_dialogue_unit(self, **kwargs):
                del kwargs
                raise AssertionError("over-budget dry run must not call Provider")

        svc = RecomputeService(
            session_factory,
            mock_bundle,
            file_index,
            tag_extractor=UnexpectedExtractor(),
        )
        baseline = SimpleNamespace(id=31, prompt_content="old canonical prompt")
        candidate = SimpleNamespace(id=41, prompt_content="new canonical prompt")

        async def load_baseline(*args, **kwargs):
            del args, kwargs
            return baseline

        async def materialize(*args, **kwargs):
            del args, kwargs
            return candidate

        async def sampled(*args, **kwargs):
            del args, kwargs
            return (11, 12)

        async def estimate(*args, **kwargs):
            del args, kwargs
            return {"provider_calls": 3, "provider_tokens": 10_001}

        monkeypatch.setattr(svc, "_load_tagger", load_baseline)
        monkeypatch.setattr(svc, "_materialize_prompt_candidate", materialize)
        monkeypatch.setattr(svc, "_stratified_dialogue_unit_ids", sampled)
        monkeypatch.setattr(svc, "_estimate_prompt_dry_run_budget", estimate)

        with pytest.raises(PromptDryRunBudgetError) as raised:
            await svc.dry_run_prompt_candidate(
                tenant_id=TENANT,
                prompt_id=7,
                prompt_version="v2",
                prompt_content="new canonical prompt",
                resolved_target=CanonicalLegacyTarget(
                    dialogue_unit_ids=(11, 12),
                    tag_keys=("intent",),
                    tagger_version_id=31,
                ),
                actor_user_id=1,
                sample_limit=100,
                max_provider_tokens=10_000,
                max_provider_calls=10,
            )

        assert raised.value.estimated_provider_tokens == 10_001
        assert raised.value.estimated_provider_calls == 3

    async def test_canonical_prompt_dry_run_binds_predictions_to_materialized_candidate(
        self,
        session_factory,
        mock_bundle,
        file_index,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """真实评估只能使用已持久化 candidate id，不能临时拼 Prompt。"""

        seen_candidate_ids: list[int] = []

        class RecordingExtractor:
            async def predict_dialogue_unit(self, **kwargs):
                seen_candidate_ids.append(int(kwargs["tagger_version_id"]))
                return SimpleNamespace(
                    assignments=(
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                        },
                    ),
                    provider_calls=1,
                    provider_input_tokens=11,
                    provider_output_tokens=3,
                )

        svc = RecomputeService(
            session_factory,
            mock_bundle,
            file_index,
            tag_extractor=RecordingExtractor(),
        )
        baseline = SimpleNamespace(id=31, prompt_content="old canonical prompt")
        candidate = SimpleNamespace(id=41, prompt_content="new canonical prompt")

        async def load_baseline(*args, **kwargs):
            del args, kwargs
            return baseline

        async def materialize(*args, **kwargs):
            del args, kwargs
            return candidate

        async def sampled(*args, **kwargs):
            del args, kwargs
            return (11, 12)

        async def estimate(*args, **kwargs):
            del args, kwargs
            return {"provider_calls": 2, "provider_tokens": 2_000}

        async def current_values(*args, **kwargs):
            del args, kwargs
            return {
                (11, "intent"): "browse",
                (12, "intent"): "purchase",
            }

        monkeypatch.setattr(svc, "_load_tagger", load_baseline)
        monkeypatch.setattr(svc, "_materialize_prompt_candidate", materialize)
        monkeypatch.setattr(svc, "_stratified_dialogue_unit_ids", sampled)
        monkeypatch.setattr(svc, "_estimate_prompt_dry_run_budget", estimate)
        monkeypatch.setattr(svc, "_current_canonical_values", current_values)

        result = await svc.dry_run_prompt_candidate(
            tenant_id=TENANT,
            prompt_id=7,
            prompt_version="v2",
            prompt_content="new canonical prompt",
            resolved_target=CanonicalLegacyTarget(
                dialogue_unit_ids=(11, 12),
                tag_keys=("intent",),
                tagger_version_id=31,
            ),
            actor_user_id=1,
            sample_limit=100,
            max_provider_tokens=2_000,
            max_provider_calls=2,
        )

        assert seen_candidate_ids == [41, 41]
        assert result["candidate_tagger_version_id"] == 41
        assert result["estimated_provider_calls"] == 2
        assert result["provider_calls"] == 2
        assert result["provider_tokens"] == 28
        assert result["changed_count"] == 1
        assert result["quality_gate_status"] == "requires_evaluation"

    async def test_get_task_status_not_found(
        self, session_factory, mock_bundle, file_index
    ) -> None:
        """get_task_status raises TaskNotFoundError for unknown task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        with pytest.raises(TaskNotFoundError):
            await svc.get_task_status("nonexistent-task", TENANT)

    async def test_get_task_status_found(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """get_task_status returns the task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        fetched = await svc.get_task_status(task.task_id, TENANT)
        assert fetched.task_id == task.task_id

    async def test_get_task_status_cross_tenant(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """get_task_status raises when accessed from different tenant."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        with pytest.raises(TaskNotFoundError):
            await svc.get_task_status(task.task_id, "byd")

    async def test_execute_task_not_found(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        file_index: FileIndex,
    ) -> None:
        """execute_task raises TaskNotFoundError for unknown task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        with pytest.raises(TaskNotFoundError):
            await svc.execute_task("nonexistent-task")

    async def test_execute_task_retires_legacy_synchronous_writer(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """Legacy tasks fail closed without mutating old tag projections."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        await svc.execute_task(task.task_id)

        fetched = await svc.get_task_status(task.task_id, TENANT)
        assert fetched.status == "failed"
        assert fetched.processed == 0
        assert fetched.finished_at is not None
        assert "canonical" in (fetched.error_message or "")

    async def test_compute_tag_value_with_cache(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """_compute_tag_value_with_cache calls LLM then caches."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        # First call — LLM
        value1, cached1 = await svc._compute_tag_value_with_cache(
            seeded_recording, "quality.greeting", "v2"
        )
        assert isinstance(value1, str)
        assert cached1 is False

        # Second call — should hit cache
        value2, cached2 = await svc._compute_tag_value_with_cache(
            seeded_recording, "quality.greeting", "v2"
        )
        assert cached2 is True
        assert value2 == value1


def test_stratified_dialogue_unit_sample_round_robins_deterministically() -> None:
    rows = [
        (1, "sales", "S1", "discover"),
        (2, "sales", "S1", "discover"),
        (3, "sales", "S2", "quote"),
        (4, "service", "S1", "close"),
        (5, "service", "S1", "close"),
    ]

    assert stratified_dialogue_unit_sample(rows, limit=3) == (1, 3, 4)
    assert stratified_dialogue_unit_sample(rows, limit=100) == (1, 3, 4, 2, 5)
