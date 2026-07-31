"""Query router — POST /query (dual-channel retrieval + rerank + answer).

See: docs/m3-prd.md §4.4.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request

from audio_graphy.api.deps import (
    get_adapters,
    get_session_factory,
    get_stores,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_any_authenticated
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.schemas.query import QueryRequest, QueryResponse
from audio_graphy.services.query import QueryService

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse, summary="Natural language query")
async def query(
    body: QueryRequest,
    request: Request,
    _user: AuthUser = Depends(require_any_authenticated()),
) -> QueryResponse:
    """Execute a dual-channel retrieval query.

    Runs naive vector search + graph channel → reranker → LLM answer generation.
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    bundle = get_adapters(request)
    stores = get_stores(request)

    svc = QueryService(
        factory,
        bundle,
        stores.vector_store,
        stores.graph_store,
        stores.file_index,
        enable_batch_judge=request.app.state.settings.enable_llm_batch_judge,
    )

    time_range = None
    if body.time_range is not None:
        time_range = (body.time_range.start, body.time_range.end)

    result = await svc.search(
        tenant_id=tenant_id,
        query=body.query,
        time_range=time_range,
        top_k=body.top_k,
        user_id=_user.id,
        permission_scope={
            "role": _user.role,
            "store_id": body.store_id,
            # Agent permissions may be identity-specific; managers/admins
            # with the same role can safely share tenant-scoped results.
            "agent_user_id": _user.id if _user.role == "agent" else None,
        },
    )

    from audio_graphy.schemas.query import Citation, RetrievalStats

    citations = [
        Citation(
            entity=c["entity"],
            chunk_id=c["chunk_id"],
            segment_ids=c["segment_ids"],
            recording_id=c["recording_id"],
            recorded_at=datetime.fromisoformat(c["recorded_at"]) if c["recorded_at"] else None,
            transcript_snippet=c["transcript_snippet"],
            confidence=c["confidence"],
        )
        for c in result["citations"]
    ]

    stats = RetrievalStats(**result["retrieval_stats"])

    return QueryResponse(
        query=result["query"],
        answer=result["answer"],
        citations=citations,
        retrieval_stats=stats,
    )
