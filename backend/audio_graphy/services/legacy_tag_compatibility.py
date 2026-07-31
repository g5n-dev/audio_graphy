"""Deterministic bridge from legacy tag mutations to canonical tag jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
from audio_graphy.models.tag_governance import (
    LegacyTagMapping,
    TagDeployment,
    TagExtractionJob,
    TaggerVersion,
    TagSchema,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceNotFoundError,
    TagGovernanceService,
    canonical_checksum,
)
from audio_graphy.services.tag_harness_runtime import resolve_harness_spec

LEGACY_RECORDING_DEFAULT_TAG_PATHS = (
    "quality.greeting",
    "quality.closing",
    "sales.product_mention",
)


@dataclass(frozen=True, slots=True)
class CanonicalLegacyTarget:
    dialogue_unit_ids: tuple[int, ...]
    tag_keys: tuple[str, ...]
    tagger_version_id: int


class LegacyTagCompatibilityService:
    """Resolve old write requests without ever mutating legacy tag tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._factory = session_factory
        self._governance = TagGovernanceService(session_factory)

    @staticmethod
    def _default_idempotency_key(
        *,
        tenant_id: str,
        operation: str,
        scope: dict[str, Any],
        tagger_version_id: int,
        prefix: str,
    ) -> str:
        checksum = canonical_checksum(
            {
                "tenant_id": tenant_id,
                "operation": operation,
                "scope": scope,
                "tagger_version_id": tagger_version_id,
            }
        )
        return f"{prefix}-{checksum[:48]}"

    @staticmethod
    def _canonical_path(path: str) -> str:
        stripped = path.strip()
        if stripped in {
            "stage",
            "intent",
            "objection",
            "next_step",
            "compliance_risk",
        }:
            return f"dialogue_tag_assignments.{stripped}"
        return stripped

    async def _resolve_recipe(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        legacy_paths: list[str],
    ) -> tuple[tuple[str, ...], int]:
        paths = tuple(sorted({self._canonical_path(path) for path in legacy_paths}))
        if not paths:
            raise GovernanceConflictError("legacy request does not contain any tag paths")
        mappings = list(
            (
                await session.execute(
                    select(LegacyTagMapping)
                    .join(
                        TagSchema,
                        (TagSchema.active_version_id == LegacyTagMapping.schema_version_id)
                        & (TagSchema.tenant_id == LegacyTagMapping.tenant_id),
                    )
                    .where(
                        LegacyTagMapping.tenant_id == tenant_id,
                        LegacyTagMapping.legacy_tag_path.in_(paths),
                        LegacyTagMapping.deterministic.is_(True),
                        TagSchema.tenant_id == tenant_id,
                        TagSchema.status == "published",
                    )
                )
            )
            .scalars()
            .all()
        )
        by_path = {mapping.legacy_tag_path: mapping for mapping in mappings}
        if set(by_path) != set(paths):
            missing = sorted(set(paths) - set(by_path))
            raise GovernanceConflictError(
                "legacy tag paths cannot be mapped deterministically; use the reception "
                f"workbench: {missing}"
            )
        schema_ids = {mapping.schema_version_id for mapping in mappings}
        if len(schema_ids) != 1:
            raise GovernanceConflictError(
                "legacy tag paths span multiple schema versions; use the reception workbench"
            )
        schema_version_id = next(iter(schema_ids))
        production = (
            await session.execute(
                select(TaggerVersion.id)
                .join(
                    TagDeployment,
                    (TagDeployment.tagger_version_id == TaggerVersion.id)
                    & (TagDeployment.tenant_id == TaggerVersion.tenant_id),
                )
                .where(
                    TaggerVersion.tenant_id == tenant_id,
                    TaggerVersion.schema_version_id == schema_version_id,
                    TagDeployment.status == "production",
                )
                .order_by(TagDeployment.approved_at.desc(), TagDeployment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        tagger_version_id = production
        if tagger_version_id is None:
            tagger_version_id = (
                await session.execute(
                    select(TaggerVersion.id)
                    .where(
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.schema_version_id == schema_version_id,
                        TaggerVersion.status == "qualified",
                    )
                    .order_by(TaggerVersion.qualified_at.desc(), TaggerVersion.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if tagger_version_id is None:
            raise GovernanceConflictError("mapped schema has no production or qualified tagger")
        return (
            tuple(sorted(mapping.tag_key for mapping in mappings)),
            int(tagger_version_id),
        )

    async def _target_reception(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        legacy_paths: list[str],
    ) -> CanonicalLegacyTarget:
        async with self._factory() as session:
            reception = (
                await session.execute(
                    select(Reception.id).where(
                        Reception.id == reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if reception is None:
                raise GovernanceNotFoundError("reception not found")
            tag_keys, tagger_version_id = await self._resolve_recipe(
                session,
                tenant_id=tenant_id,
                legacy_paths=legacy_paths,
            )
            dialogue_unit_ids = tuple(
                (
                    await session.execute(
                        select(DialogueUnit.id)
                        .where(
                            DialogueUnit.tenant_id == tenant_id,
                            DialogueUnit.reception_id == reception_id,
                        )
                        .order_by(DialogueUnit.unit_index, DialogueUnit.id)
                    )
                )
                .scalars()
                .all()
            )
        if not dialogue_unit_ids:
            raise GovernanceConflictError(
                "reception has no dialogue units; complete segmentation in the workbench first"
            )
        return CanonicalLegacyTarget(
            dialogue_unit_ids=dialogue_unit_ids,
            tag_keys=tag_keys,
            tagger_version_id=tagger_version_id,
        )

    async def _target_recordings(
        self,
        *,
        tenant_id: str,
        recording_ids: list[int],
        legacy_paths: list[str],
    ) -> CanonicalLegacyTarget:
        if not recording_ids:
            raise GovernanceConflictError(
                "recording scope is required for deterministic legacy migration"
            )
        async with self._factory() as session:
            tag_keys, tagger_version_id = await self._resolve_recipe(
                session,
                tenant_id=tenant_id,
                legacy_paths=legacy_paths,
            )
            unit_ids: list[int] = []
            for recording_id in sorted(set(recording_ids)):
                reception_ids = tuple(
                    (
                        await session.execute(
                            select(ReceptionRecording.reception_id)
                            .where(
                                ReceptionRecording.tenant_id == tenant_id,
                                ReceptionRecording.recording_id == recording_id,
                            )
                            .distinct()
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(reception_ids) != 1:
                    raise GovernanceConflictError(
                        f"recording {recording_id} does not map to exactly one reception; "
                        "use the reception workbench"
                    )
                candidates = tuple(
                    (
                        await session.execute(
                            select(DialogueUnit.id)
                            .where(
                                DialogueUnit.tenant_id == tenant_id,
                                DialogueUnit.reception_id == reception_ids[0],
                                DialogueUnit.source_recording_id == recording_id,
                            )
                            .order_by(DialogueUnit.unit_index, DialogueUnit.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(candidates) != 1:
                    raise GovernanceConflictError(
                        f"recording {recording_id} maps to {len(candidates)} dialogue units; "
                        "legacy recording tags cannot be copied across a reception"
                    )
                unit_ids.append(candidates[0])
        return CanonicalLegacyTarget(
            dialogue_unit_ids=tuple(sorted(set(unit_ids))),
            tag_keys=tag_keys,
            tagger_version_id=tagger_version_id,
        )

    async def resolve_recordings(
        self,
        *,
        tenant_id: str,
        recording_ids: list[int],
        legacy_paths: list[str],
    ) -> CanonicalLegacyTarget:
        """Validate a legacy recording scope without creating a job."""

        return await self._target_recordings(
            tenant_id=tenant_id,
            recording_ids=recording_ids,
            legacy_paths=legacy_paths,
        )

    async def resolve_prompt_scope(
        self,
        *,
        tenant_id: str,
        legacy_paths: list[str],
    ) -> CanonicalLegacyTarget:
        """Resolve the canonical recipe and all dialogue-unit evaluation subjects."""

        async with self._factory() as session:
            tag_keys, tagger_version_id = await self._resolve_recipe(
                session,
                tenant_id=tenant_id,
                legacy_paths=legacy_paths,
            )
            dialogue_unit_ids = tuple(
                int(value)
                for value in (
                    (
                        await session.execute(
                            select(DialogueUnit.id)
                            .where(DialogueUnit.tenant_id == tenant_id)
                            .order_by(DialogueUnit.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        return CanonicalLegacyTarget(
            dialogue_unit_ids=dialogue_unit_ids,
            tag_keys=tag_keys,
            tagger_version_id=tagger_version_id,
        )

    async def validate_prompt_candidate(
        self,
        *,
        tenant_id: str,
        resolved_target: CanonicalLegacyTarget,
        candidate_tagger_version_id: int,
        prompt_content: str,
    ) -> CanonicalLegacyTarget:
        """Require a matching, quality-gated production recipe before activation."""

        if int(candidate_tagger_version_id) != int(resolved_target.tagger_version_id):
            raise GovernanceConflictError(
                "Prompt candidate is not the current production TaggerVersion; "
                "complete canonical evaluation and deployment first"
            )
        async with self._factory() as session:
            candidate = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == candidate_tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if candidate is None:
                raise GovernanceConflictError("Prompt candidate TaggerVersion does not exist")
            production = (
                await session.execute(
                    select(TagDeployment.id)
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.tagger_version_id == candidate_tagger_version_id,
                        TagDeployment.status == "production",
                    )
                    .order_by(TagDeployment.approved_at.desc(), TagDeployment.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if candidate.status != "qualified" or production is None:
            raise GovernanceConflictError(
                "Prompt candidate has not passed the canonical quality gates "
                "and production deployment"
            )
        if candidate.engine not in {"llm", "hybrid"}:
            raise GovernanceConflictError(
                "the production TaggerVersion does not consume Prompt.content"
            )
        normalized_prompt = prompt_content.replace("\r\n", "\n").replace("\r", "\n").strip()
        candidate_prompt = (
            str(candidate.prompt_content).replace("\r\n", "\n").replace("\r", "\n").strip()
        )
        harness_prompt = (
            str(resolve_harness_spec(candidate)["generation"]["prompt_template"])
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        if normalized_prompt != candidate_prompt or normalized_prompt != harness_prompt:
            raise GovernanceConflictError(
                "Prompt.content does not match the production TaggerVersion recipe"
            )
        return resolved_target

    async def enqueue_reception(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        legacy_paths: list[str],
        actor_user_id: int,
        idempotency_key: str | None = None,
    ) -> TagExtractionJob:
        target = await self._target_reception(
            tenant_id=tenant_id,
            reception_id=reception_id,
            legacy_paths=legacy_paths,
        )
        scope: dict[str, Any] = {
            "dialogue_unit_ids": list(target.dialogue_unit_ids),
            "target_tag_keys": list(target.tag_keys),
            "compatibility_source": "legacy_dialogue_tags_derive",
            "reception_id": reception_id,
        }
        return await self._governance.enqueue_job(
            tenant_id=tenant_id,
            job_type="extract",
            scope=scope,
            idempotency_key=idempotency_key
            or self._default_idempotency_key(
                tenant_id=tenant_id,
                operation="legacy_dialogue_tags_derive",
                scope=scope,
                tagger_version_id=target.tagger_version_id,
                prefix="legacy-derive",
            ),
            created_by=actor_user_id,
            tagger_version_id=target.tagger_version_id,
            origin="backfill",
        )

    async def enqueue_recordings(
        self,
        *,
        tenant_id: str,
        recording_ids: list[int],
        legacy_paths: list[str],
        actor_user_id: int,
        operation: str,
        idempotency_key: str | None = None,
        resolved_target: CanonicalLegacyTarget | None = None,
        prompt_id: int | None = None,
    ) -> TagExtractionJob:
        target = resolved_target or await self.resolve_recordings(
            tenant_id=tenant_id,
            recording_ids=recording_ids,
            legacy_paths=legacy_paths,
        )
        scope: dict[str, Any] = {
            "dialogue_unit_ids": list(target.dialogue_unit_ids),
            "target_tag_keys": list(target.tag_keys),
            "compatibility_source": operation,
            "legacy_recording_ids": sorted(set(recording_ids)),
        }
        if operation == "legacy_prompt_activation":
            scope["prompt_candidate_tagger_version_id"] = target.tagger_version_id
            if prompt_id is not None:
                scope["prompt_id"] = prompt_id
        return await self._governance.enqueue_job(
            tenant_id=tenant_id,
            job_type=(
                "recompute"
                if operation in {"legacy_recompute", "legacy_prompt_activation"}
                else "extract"
            ),
            scope=scope,
            idempotency_key=idempotency_key
            or self._default_idempotency_key(
                tenant_id=tenant_id,
                operation=operation,
                scope=scope,
                tagger_version_id=target.tagger_version_id,
                prefix=operation,
            ),
            created_by=actor_user_id,
            tagger_version_id=target.tagger_version_id,
            origin="backfill",
        )


__all__ = [
    "LEGACY_RECORDING_DEFAULT_TAG_PATHS",
    "CanonicalLegacyTarget",
    "LegacyTagCompatibilityService",
]
