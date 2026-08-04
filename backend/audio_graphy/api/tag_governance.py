"""REST API for the versioned tag-governance closed loop."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy import select

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin, require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import APIError
from audio_graphy.models.tag_governance import TagDeployment, TaggerVersion
from audio_graphy.schemas.tag_governance import (
    GoldSetCreate,
    GoldSetFreeze,
    OptimizationCandidateCompare,
    OptimizationCreate,
    OptimizationRunCreate,
    ReviewBatchCreate,
    ReviewDecisionCreate,
    ReviewReleaseCreate,
    TagDeploymentCreate,
    TagDeploymentResumeCreate,
    TagEvaluationCreate,
    TaggerVersionCreate,
    TagJobCreate,
    TagRollbackCreate,
    TagSchemaCreate,
    TagSchemaVersionCreate,
)
from audio_graphy.services.tag_evaluator import TagEvaluationService
from audio_graphy.services.tag_governance import (
    AssignmentValidationError,
    GovernanceConflictError,
    GovernanceError,
    GovernanceNotFoundError,
    TagGovernanceService,
    canonical_checksum,
)

router = APIRouter(tags=["tag governance"])


def _service(request: Request) -> TagGovernanceService:
    return TagGovernanceService(get_session_factory(request))


def _normalized_job_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize set-like public scope fields before hashing and persistence."""

    normalized = dict(scope)
    for field_name in ("dialogue_unit_ids", "reception_ids"):
        values = normalized.get(field_name)
        if isinstance(values, list) and all(
            isinstance(item, int) and not isinstance(item, bool) for item in values
        ):
            normalized[field_name] = sorted(set(values))
    target_tag_keys = normalized.get("target_tag_keys")
    if isinstance(target_tag_keys, list) and all(isinstance(item, str) for item in target_tag_keys):
        normalized["target_tag_keys"] = sorted({item.strip() for item in target_tag_keys})
    return normalized


def _stable_default_idempotency_key(
    *,
    tenant_id: str,
    operation: str,
    scope: dict[str, Any],
    tagger_version_id: int | None,
) -> str:
    return (
        "stable-"
        + canonical_checksum(
            {
                "tenant_id": tenant_id,
                "operation": operation,
                "scope": scope,
                "tagger_version_id": tagger_version_id,
            }
        )[:48]
    )


async def _resolve_default_tagger_version_id(
    request: Request,
    *,
    tenant_id: str,
) -> int | None:
    """Bind a public job to the same server-owned route the worker would choose."""

    async with get_session_factory(request)() as session:
        production = (
            await session.execute(
                select(TagDeployment.tagger_version_id)
                .where(
                    TagDeployment.tenant_id == tenant_id,
                    TagDeployment.status == "production",
                )
                .order_by(TagDeployment.approved_at.desc(), TagDeployment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if production is not None:
            return int(production)
        active_baseline = (
            await session.execute(
                select(TagDeployment.baseline_tagger_version_id)
                .where(
                    TagDeployment.tenant_id == tenant_id,
                    TagDeployment.status.in_(["shadow", "canary_5", "canary_25", "awaiting_admin"]),
                )
                .order_by(TagDeployment.created_at.desc(), TagDeployment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if active_baseline is not None:
            return int(active_baseline)
        rollback_baseline = (
            await session.execute(
                select(TagDeployment.baseline_tagger_version_id)
                .where(
                    TagDeployment.tenant_id == tenant_id,
                    TagDeployment.status == "rolled_back",
                )
                .order_by(TagDeployment.rolled_back_at.desc(), TagDeployment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if rollback_baseline is not None:
            return int(rollback_baseline)
        qualified = (
            await session.execute(
                select(TaggerVersion.id)
                .where(
                    TaggerVersion.tenant_id == tenant_id,
                    TaggerVersion.status == "qualified",
                )
                .order_by(TaggerVersion.created_at.desc(), TaggerVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return int(qualified) if qualified is not None else None


async def _domain[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except GovernanceNotFoundError as exc:
        raise APIError(
            str(exc),
            code="TAG_GOVERNANCE_NOT_FOUND",
            status_code=404,
        ) from exc
    except GovernanceConflictError as exc:
        raise APIError(
            str(exc),
            code="TAG_GOVERNANCE_CONFLICT",
            status_code=409,
        ) from exc
    except (AssignmentValidationError, GovernanceError) as exc:
        raise APIError(
            str(exc),
            code="TAG_GOVERNANCE_VALIDATION",
            status_code=422,
        ) from exc


def _resource(model: Any) -> dict[str, Any]:
    return cast(dict[str, Any], model.to_dict())


def _items(models: list[Any]) -> dict[str, Any]:
    return {"items": [_resource(model) for model in models], "total": len(models)}


def _job_resource(model: Any) -> dict[str, Any]:
    """Never expose service-owned sampling manifests through the public job API."""

    resource = _resource(model)
    if resource.get("job_type") not in {"extract", "recompute"}:
        resource["scope"] = {}
        resource["idempotency_key"] = None
        resource["failed_subset"] = []
    return resource


def _review_resource(
    model: Any,
    *,
    viewer_user_id: int | None = None,
    mask_pending_semantics: bool = False,
) -> dict[str, Any]:
    """Hide model-derived hints until a blind review has been submitted."""

    resource = _resource(model)
    blind_unresolved = bool(resource.get("blind_mode")) and resource.get("status") != "resolved"
    safe_pending_queue = mask_pending_semantics and resource.get("status") == "pending"
    if blind_unresolved or safe_pending_queue:
        resource.update(
            {
                "proposed_value": None,
                "confidence": None,
                "evidence_refs": [],
                "proposed_fact_id": None,
                "tagger_version_id": None,
                "source_deployment_id": None,
                "source_extraction_run_id": None,
                "source_harness_execution_id": None,
                "sampled_deployment_stage": None,
                "sampled_deployment_revision": None,
                "sampling_manifest_checksum": None,
                "batch_id": None,
                "review_bundle_id": None,
                "created_by": None,
            }
        )
        if blind_unresolved and (
            resource.get("status") == "pending" or resource.get("claimed_by") != viewer_user_id
        ):
            resource.update(
                {
                    "subject_id": None,
                    "reception_id": None,
                    "schema_version_id": None,
                }
            )
    return resource


async def _deny_blind_review_side_channel(
    request: Request,
    *,
    user: AuthUser,
) -> None:
    if not await _service(request).record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind="tag_governance_global",
    ):
        raise APIError(
            "该盲审任务提交前不可访问模型输出、历史结论或执行谱系",
            code="BLIND_REVIEW_ISOLATION",
            status_code=403,
        )


def _if_match_revision(value: str) -> int:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise APIError(
            "If-Match must contain the current integer revision",
            code="TAG_GOVERNANCE_IF_MATCH_INVALID",
            status_code=422,
        ) from exc
    if revision <= 0:
        raise APIError(
            "If-Match revision must be positive",
            code="TAG_GOVERNANCE_IF_MATCH_INVALID",
            status_code=422,
        )
    return revision


@router.post(
    "/tag-schemas",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def create_tag_schema(
    body: TagSchemaCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    schema = await _domain(
        _service(request).create_schema(
            tenant_id=get_tenant_id(request),
            key=body.key,
            name=body.name,
            description=body.description,
            created_by=user.id,
        )
    )
    return _resource(schema)


@router.get("/tag-schemas")
async def list_tag_schemas(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    rows = await _domain(
        _service(request).list_schemas(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    versions = await _domain(
        _service(request).list_schema_versions(
            tenant_id=get_tenant_id(request),
            limit=500,
        )
    )
    can_govern = user.role in {"admin", "inspector"}
    if not can_govern:
        rows = [row for row in rows if row.status == "published"]
        versions = [version for version in versions if version.status == "published"]
    versions_by_schema: dict[int, list[dict[str, Any]]] = {}
    for version in versions:
        versions_by_schema.setdefault(version.schema_id, []).append(_resource(version))
    items = [{**_resource(row), "versions": versions_by_schema.get(row.id, [])} for row in rows]
    return {"items": items, "total": len(items)}


@router.get("/tag-schemas/{schema_id}")
async def get_tag_schema(
    schema_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).get_schema(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
        )
    )
    versions = await _domain(
        _service(request).list_schema_versions(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
        )
    )
    if user.role not in {"admin", "inspector"}:
        if row.status != "published":
            raise APIError(
                "Tag schema not found",
                code="TAG_GOVERNANCE_NOT_FOUND",
                status_code=404,
            )
        versions = [version for version in versions if version.status == "published"]
    return {**_resource(row), "versions": [_resource(version) for version in versions]}


@router.get("/tag-schemas/{schema_id}/versions")
async def list_tag_schema_versions(
    schema_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _domain(
        _service(request).get_schema(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
        )
    )
    rows = await _domain(
        _service(request).list_schema_versions(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
            limit=limit,
        )
    )
    if user.role not in {"admin", "inspector"}:
        rows = [row for row in rows if row.status == "published"]
    return _items(rows)


@router.post(
    "/tag-schemas/{schema_id}/versions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def create_tag_schema_version(
    schema_id: int,
    body: TagSchemaVersionCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).create_schema_version(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
            version=body.version,
            definitions=[definition.model_dump(mode="json") for definition in body.definitions],
            created_by=user.id,
        )
    )
    return _resource(row)


@router.post(
    "/tag-schemas/{schema_id}/versions/{version_id}/publish",
    dependencies=[Depends(require_admin())],
)
async def publish_tag_schema_version(
    schema_id: int,
    version_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).publish_schema_version(
            tenant_id=get_tenant_id(request),
            schema_id=schema_id,
            version_id=version_id,
            actor_user_id=user.id,
        )
    )
    return _resource(row)


@router.post(
    "/tagger-versions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def create_tagger_version(
    body: TaggerVersionCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).create_tagger_version(
            tenant_id=get_tenant_id(request),
            schema_version_id=body.schema_version_id,
            version=body.version,
            engine=body.engine,
            prompt_content=body.prompt_content,
            rule_bundle=body.rule_bundle,
            model_version=body.model_version,
            thresholds=body.thresholds,
            harness_spec=(
                body.harness_spec.model_dump(mode="json") if body.harness_spec is not None else None
            ),
            parent_version_id=body.parent_version_id,
            origin="manual",
            optimization_run_id=None,
            change_summary=body.change_summary,
            created_by=user.id,
        )
    )
    return _resource(row)


@router.get(
    "/tagger-versions",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tagger_versions(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_tagger_versions(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return _items(rows)


@router.post(
    "/tagger-versions/optimize",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def optimize_tagger_version(
    body: OptimizationCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    candidate, metadata = await _domain(
        _service(request).create_server_bound_optimization_candidate(
            tenant_id=get_tenant_id(request),
            gold_set_version_id=body.gold_set_version_id,
            actor_user_id=user.id,
        )
    )
    return {"candidate": _resource(candidate), "optimization": metadata}


@router.get(
    "/tag-evolution/overview",
    dependencies=[Depends(require_inspector_or_above())],
)
async def get_tag_evolution_overview(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    return await _domain(
        _service(request).get_evolution_overview(
            tenant_id=get_tenant_id(request),
        )
    )


@router.get(
    "/tag-badcases",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_badcases(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_badcases(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return _items(rows)


@router.post(
    "/tag-optimization-runs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin())],
)
async def create_tag_optimization_run(
    body: OptimizationRunCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    run = await _domain(
        _service(request).create_server_bound_optimization_run(
            tenant_id=get_tenant_id(request),
            cohort=body.cohort.model_dump(mode="json"),
            target_policy=body.target_policy.model_dump(mode="json"),
            search_budget=body.search_budget.model_dump(mode="json"),
            actor_user_id=user.id,
        )
    )
    return _resource(run)


@router.get(
    "/tag-optimization-runs",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_optimization_runs(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_optimization_runs(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return _items(rows)


@router.get(
    "/tag-optimization-runs/{optimization_run_id}",
    dependencies=[Depends(require_inspector_or_above())],
)
async def get_tag_optimization_run(
    optimization_run_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    run, trials = await _domain(
        _service(request).get_optimization_run(
            tenant_id=get_tenant_id(request),
            optimization_run_id=optimization_run_id,
        )
    )
    resource = _resource(run)
    resource["trials"] = [_resource(trial) for trial in trials]
    return resource


@router.post(
    "/tag-optimization-runs/{optimization_run_id}/compare",
    dependencies=[Depends(require_inspector_or_above())],
)
async def compare_tag_optimization_trials(
    optimization_run_id: int,
    body: OptimizationCandidateCompare,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    return await _domain(
        _service(request).compare_optimization_trials(
            tenant_id=get_tenant_id(request),
            optimization_run_id=optimization_run_id,
            left_trial_id=body.left_trial_id,
            right_trial_id=body.right_trial_id,
        )
    )


@router.post(
    "/tag-optimization-runs/{optimization_run_id}/cancel",
    dependencies=[Depends(require_admin())],
)
async def cancel_tag_optimization_run(
    optimization_run_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    run = await _domain(
        _service(request).cancel_optimization_run(
            tenant_id=get_tenant_id(request),
            optimization_run_id=optimization_run_id,
            actor_user_id=user.id,
        )
    )
    return _resource(run)


@router.get(
    "/tag-harness-executions/{harness_execution_id}",
    dependencies=[Depends(require_inspector_or_above())],
)
async def get_tag_harness_execution(
    harness_execution_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    service = _service(request)
    if not await service.record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind="harness_execution",
    ):
        raise APIError(
            "该盲审任务提交前不可访问执行谱系",
            code="BLIND_REVIEW_ISOLATION",
            status_code=403,
        )
    execution, traces = await _domain(
        service.get_harness_execution(
            tenant_id=get_tenant_id(request),
            harness_execution_id=harness_execution_id,
        )
    )
    resource = _resource(execution)
    resource["traces"] = [_resource(trace) for trace in traces]
    return resource


@router.post(
    "/tag-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_inspector_or_above())],
)
async def create_tag_job(
    body: TagJobCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    scope = _normalized_job_scope(body.scope)
    tagger_version_id = await _resolve_default_tagger_version_id(
        request,
        tenant_id=tenant_id,
    )
    client_key = idempotency_key or _stable_default_idempotency_key(
        tenant_id=tenant_id,
        operation=body.job_type,
        scope=scope,
        tagger_version_id=tagger_version_id,
    )
    key = f"public-tag-job:{client_key}"
    row = await _domain(
        _service(request).enqueue_job(
            tenant_id=tenant_id,
            job_type=body.job_type,
            scope=scope,
            idempotency_key=key,
            created_by=user.id,
            tagger_version_id=tagger_version_id,
            origin="manual",
        )
    )
    return _job_resource(row)


@router.get(
    "/tag-jobs",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_jobs(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_jobs(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return {"items": [_job_resource(row) for row in rows], "total": len(rows)}


@router.get(
    "/tag-jobs/{job_id}",
    dependencies=[Depends(require_inspector_or_above())],
)
async def get_tag_job(
    job_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    row = await _domain(_service(request).get_job(tenant_id=get_tenant_id(request), job_id=job_id))
    return _job_resource(row)


@router.get("/tag-facts/{fact_id}/lineage")
async def get_tag_fact_lineage(
    fact_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    service = _service(request)
    if not await service.record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind="fact_lineage",
    ):
        raise APIError(
            "该盲审任务提交前不可访问事实谱系",
            code="BLIND_REVIEW_ISOLATION",
            status_code=403,
        )
    bundle = await _domain(
        service.get_fact_lineage(
            tenant_id=get_tenant_id(request),
            fact_id=fact_id,
            actor_user_id=user.id,
            actor_role=user.role,
        )
    )
    result = {
        key: (_resource(value) if value is not None and hasattr(value, "to_dict") else value)
        for key, value in bundle.items()
    }
    if user.role not in {"admin", "inspector"}:
        tagger = result.get("tagger_version")
        if isinstance(tagger, dict):
            result["tagger_version"] = {
                key: tagger.get(key)
                for key in (
                    "id",
                    "schema_version_id",
                    "version",
                    "engine",
                    "model_version",
                    "status",
                )
            }
        job = result.get("job")
        if isinstance(job, dict):
            result["job"] = {
                key: job.get(key)
                for key in ("id", "job_type", "status", "created_at", "finished_at")
            }
    return result


@router.post(
    "/tag-jobs/{job_id}/retry",
    dependencies=[Depends(require_inspector_or_above())],
)
async def retry_tag_job(
    job_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    current = await _domain(service.get_job(tenant_id=get_tenant_id(request), job_id=job_id))
    if user.role != "admin" and current.job_type not in {"extract", "recompute"}:
        raise APIError(
            "内部评估、优化、复核与修复作业只能由管理员控制",
            code="TAG_JOB_INTERNAL_ADMIN_REQUIRED",
            status_code=403,
        )
    row = await _domain(
        service.retry_job(
            tenant_id=get_tenant_id(request),
            job_id=job_id,
            actor_user_id=user.id,
        )
    )
    return _job_resource(row)


@router.post(
    "/tag-jobs/{job_id}/cancel",
    dependencies=[Depends(require_inspector_or_above())],
)
async def cancel_tag_job(
    job_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    current = await _domain(service.get_job(tenant_id=get_tenant_id(request), job_id=job_id))
    if user.role != "admin" and current.job_type not in {"extract", "recompute"}:
        raise APIError(
            "内部评估、优化、复核与修复作业只能由管理员控制",
            code="TAG_JOB_INTERNAL_ADMIN_REQUIRED",
            status_code=403,
        )
    row = await _domain(
        service.cancel_job(
            tenant_id=get_tenant_id(request),
            job_id=job_id,
            actor_user_id=user.id,
        )
    )
    return _job_resource(row)


@router.post(
    "/tag-reviews/create-batch",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_inspector_or_above())],
)
async def create_review_batch(
    body: ReviewBatchCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    server_policy = {
        "critical": ("critical_positive", True, None),
        "gold": ("gold_matrix", True, None),
        "random": ("manual_random", True, None),
        "audit": ("manual_audit", True, None),
    }.get(body.reason, ("active_learning", False, None))
    selection_policy, blind_mode, sampling_probability = server_policy
    rows = await _domain(
        _service(request).create_review_batch(
            tenant_id=get_tenant_id(request),
            reason=body.reason,
            subjects=[subject.model_dump(mode="json") for subject in body.subjects],
            actor_user_id=user.id,
            review_bundle_id=body.review_bundle_id,
            selection_policy=selection_policy,
            selection_policy_version="1",
            sampling_probability=sampling_probability,
            blind_mode=blind_mode,
        )
    )
    return {
        "batch_id": rows[0].batch_id,
        # The gold-set cohort selects tasks by review_bundle_id, NOT by batch_id.
        # Returning only the batch id sent operators to freeze an empty cohort.
        "review_bundle_id": rows[0].review_bundle_id,
        "created_count": len(rows),
        "items": [_review_resource(row, viewer_user_id=user.id) for row in rows],
    }


@router.get(
    "/tag-reviews",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_reviews(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    review_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    service = _service(request)
    rows = await _domain(
        service.list_reviews_for_viewer(
            tenant_id=get_tenant_id(request),
            reviewer_user_id=user.id,
            status=review_status,
            limit=limit,
        )
    )
    return {
        "items": [
            _review_resource(
                row,
                viewer_user_id=user.id,
                mask_pending_semantics=(review_status in {None, "active"}),
            )
            for row in rows
        ],
        "total": len(rows),
    }


@router.post(
    "/tag-reviews/{task_id}/claim",
    dependencies=[Depends(require_inspector_or_above())],
)
async def claim_tag_review(
    task_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).claim_review(
            tenant_id=get_tenant_id(request),
            task_id=task_id,
            reviewer_user_id=user.id,
        )
    )
    return _review_resource(row, viewer_user_id=user.id)


@router.post(
    "/tag-reviews/{task_id}/release",
    dependencies=[Depends(require_inspector_or_above())],
)
async def release_tag_review(
    task_id: int,
    body: ReviewReleaseCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    if body.force and user.role != "admin":
        raise APIError(
            "only admins can force-release another reviewer's task",
            code="TAG_REVIEW_FORCE_RELEASE_FORBIDDEN",
            status_code=403,
        )
    row = await _domain(
        _service(request).release_review(
            tenant_id=get_tenant_id(request),
            task_id=task_id,
            actor_user_id=user.id,
            force=body.force,
        )
    )
    return _review_resource(row, viewer_user_id=user.id)


@router.post(
    "/tag-reviews/{task_id}/decide",
    dependencies=[Depends(require_inspector_or_above())],
)
async def decide_tag_review(
    task_id: int,
    body: ReviewDecisionCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    task, decision, fact = await _domain(
        _service(request).decide_review(
            tenant_id=get_tenant_id(request),
            task_id=task_id,
            reviewer_user_id=user.id,
            action=body.action,
            corrected_value=body.corrected_value,
            reason_code=body.reason_code,
            note=body.note,
            evidence_refs=body.evidence_refs,
            adjudication=False,
            truth_state=body.truth_state,
            truth_tier="t1",
            annotator_round=1,
            primary_failure_stage=body.primary_failure_stage,
            reason_codes=body.reason_codes,
            reviewer_confidence=body.reviewer_confidence,
            review_duration_ms=body.review_duration_ms,
        )
    )
    return {
        "task": _resource(task),
        "decision": _resource(decision),
        "fact": _resource(fact) if fact is not None else None,
    }


@router.post(
    "/tag-reviews/{task_id}/adjudicate",
    dependencies=[Depends(require_inspector_or_above())],
)
async def adjudicate_tag_review(
    task_id: int,
    body: ReviewDecisionCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Resolve an already-claimed task through the explicit arbitration path."""

    task, decision, fact = await _domain(
        _service(request).decide_review(
            tenant_id=get_tenant_id(request),
            task_id=task_id,
            reviewer_user_id=user.id,
            action=body.action,
            corrected_value=body.corrected_value,
            reason_code=body.reason_code,
            note=body.note,
            evidence_refs=body.evidence_refs,
            adjudication=True,
            truth_state=body.truth_state,
            truth_tier="t3",
            annotator_round=3,
            primary_failure_stage=body.primary_failure_stage,
            reason_codes=body.reason_codes,
            reviewer_confidence=body.reviewer_confidence,
            review_duration_ms=body.review_duration_ms,
        )
    )
    return {
        "task": _resource(task),
        "decision": _resource(decision),
        "fact": _resource(fact) if fact is not None else None,
    }


@router.post(
    "/tag-gold-sets",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_inspector_or_above())],
)
async def create_gold_set(
    body: GoldSetCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).create_gold_set(
            tenant_id=get_tenant_id(request),
            key=body.key,
            name=body.name,
            description=body.description,
            schema_version_id=body.schema_version_id,
            actor_user_id=user.id,
        )
    )
    return _resource(row)


@router.get(
    "/tag-gold-sets",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_gold_sets(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_gold_sets(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return _items(rows)


@router.post(
    "/tag-gold-sets/{gold_set_id}/freeze",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_inspector_or_above())],
)
async def freeze_gold_set(
    gold_set_id: int,
    body: GoldSetFreeze,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).freeze_gold_set(
            tenant_id=get_tenant_id(request),
            gold_set_id=gold_set_id,
            version=body.version,
            decision_ids=[],
            cohort=body.cohort.model_dump(mode="json"),
            require_complete=True,
            actor_user_id=user.id,
        )
    )
    return _resource(row)


_SEALED_AGGREGATE_METRICS = frozenset(
    {
        "macro_f1",
        "critical_recall",
        "critical_recall_lcb",
        "critical_lcb_enforced",
        "critical_positive_support",
        "evidence_coverage",
        "evidence_iou",
        "brier_score",
        "ece",
        "calibration_support",
        "schema_violation_count",
        "evidence_violation_count",
        "lineage_violation_count",
        "error_rate",
        "subject_count",
        "evaluation_lane",
        "holdout_only",
        "sealed_release",
        "mixed_denominator_release_gate",
        "paired_accuracy",
    }
)


def _evaluation_resource(run: Any, gates: Any) -> dict[str, Any]:
    resource = _resource(run)
    metrics = resource.get("metrics")
    if isinstance(metrics, dict) and bool(metrics.get("sealed_release")):
        resource["metrics"] = {
            key: value for key, value in metrics.items() if key in _SEALED_AGGREGATE_METRICS
        }
        baseline_metrics = resource.get("baseline_metrics")
        resource["baseline_metrics"] = (
            {
                key: value
                for key, value in baseline_metrics.items()
                if key in _SEALED_AGGREGATE_METRICS
            }
            if isinstance(baseline_metrics, dict)
            else {}
        )
        resource["sealed_details_redacted"] = True
        return {
            **resource,
            "gates": [
                {
                    "code": "sealed_release",
                    "passed": bool(resource.get("passed")),
                    "actual": None,
                    "threshold": None,
                    "message": "Sealed Holdout 仅公开聚合结果",
                }
            ],
        }
    return {
        **resource,
        "gates": [
            (
                _resource(gate)
                if hasattr(gate, "to_dict")
                else {
                    "code": gate.code,
                    "passed": gate.passed,
                    "actual": gate.actual,
                    "threshold": gate.threshold,
                    "message": gate.message,
                }
            )
            for gate in gates
        ],
    }


@router.post(
    "/tag-evaluations",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_inspector_or_above())],
)
async def create_tag_evaluation(
    body: TagEvaluationCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    evaluation_scope = {
        "tagger_version_id": body.tagger_version_id,
        "gold_set_version_id": body.gold_set_version_id,
        "baseline_tagger_version_id": body.baseline_tagger_version_id,
    }
    client_key = idempotency_key or _stable_default_idempotency_key(
        tenant_id=tenant_id,
        operation="evaluate",
        scope=evaluation_scope,
        tagger_version_id=body.tagger_version_id,
    )
    key = f"public-evaluation:{client_key}"
    run, job = await _domain(
        TagEvaluationService(get_session_factory(request)).enqueue(
            tenant_id=tenant_id,
            tagger_version_id=body.tagger_version_id,
            gold_set_version_id=body.gold_set_version_id,
            baseline_tagger_version_id=body.baseline_tagger_version_id,
            idempotency_key=key,
            actor_user_id=user.id,
        )
    )
    return {
        "job_id": job.id,
        "evaluation": _evaluation_resource(run, []),
    }


@router.get(
    "/tag-evaluations",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_evaluations(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_evaluations(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    items = [_evaluation_resource(run, gates) for run, gates in rows]
    return {"items": items, "total": len(items)}


@router.post(
    "/tag-deployments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def create_tag_deployment(
    body: TagDeploymentCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).create_deployment(
            tenant_id=get_tenant_id(request),
            tagger_version_id=body.tagger_version_id,
            evaluation_run_id=body.evaluation_run_id,
            baseline_tagger_version_id=body.baseline_tagger_version_id,
            actor_user_id=user.id,
        )
    )
    return _resource(row)


@router.get(
    "/tag-deployments",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_deployments(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_deployments(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    return _items(rows)


@router.post(
    "/tag-deployments/{deployment_id}/promote",
    dependencies=[Depends(require_admin())],
)
async def promote_tag_deployment(
    deployment_id: int,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).transition_deployment(
            tenant_id=get_tenant_id(request),
            deployment_id=deployment_id,
            action="promote",
            actor_user_id=user.id,
            expected_revision=_if_match_revision(if_match),
        )
    )
    return _resource(row)


@router.post(
    "/tag-deployments/{deployment_id}/approve",
    dependencies=[Depends(require_admin())],
)
async def approve_tag_deployment(
    deployment_id: int,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).transition_deployment(
            tenant_id=get_tenant_id(request),
            deployment_id=deployment_id,
            action="approve",
            actor_user_id=user.id,
            expected_revision=_if_match_revision(if_match),
        )
    )
    return _resource(row)


@router.post(
    "/tag-deployments/{deployment_id}/resume",
    dependencies=[Depends(require_admin())],
)
async def resume_tag_deployment(
    deployment_id: int,
    body: TagDeploymentResumeCreate,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).transition_deployment(
            tenant_id=get_tenant_id(request),
            deployment_id=deployment_id,
            action="resume",
            actor_user_id=user.id,
            expected_revision=_if_match_revision(if_match),
            reason=body.reason,
        )
    )
    return _resource(row)


@router.post(
    "/tag-deployments/{deployment_id}/rollback",
    dependencies=[Depends(require_admin())],
)
async def rollback_tag_deployment(
    deployment_id: int,
    body: TagRollbackCreate,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    row = await _domain(
        _service(request).transition_deployment(
            tenant_id=get_tenant_id(request),
            deployment_id=deployment_id,
            action="rollback",
            actor_user_id=user.id,
            expected_revision=_if_match_revision(if_match),
            reason=body.reason,
        )
    )
    return _resource(row)


@router.get(
    "/tag-deployments/{deployment_id}/observations",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_deployment_observations(
    deployment_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=1_000)] = 200,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_deployment_observations(
            tenant_id=get_tenant_id(request),
            deployment_id=deployment_id,
            limit=limit,
        )
    )
    return _items(rows)


@router.get(
    "/tag-audit-events",
    dependencies=[Depends(require_inspector_or_above())],
)
async def list_tag_audit_events(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_audit_events(
            tenant_id=get_tenant_id(request),
            limit=limit,
        )
    )
    items = []
    for row in rows:
        item = _resource(row)
        item["resource_id"] = str(item["resource_id"])
        items.append(item)
    return {"items": items, "total": len(items)}


__all__ = ["router"]
