"""API-key administration — the JWT-authenticated side of the open API.

Admin-only. The create response is the only place the key plaintext and the
webhook signing secret ever appear: the database keeps a SHA-256 of the key
and derives the secret, so neither can be shown again. Revocation is a flag,
not a delete — uploads reference the key row (RESTRICT) and an audit trail
that loses its principal is not an audit trail.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import APIError
from audio_graphy.models.integration import ApiKey
from audio_graphy.services.integration import (
    derive_webhook_secret,
    generate_api_key,
    load_signing_root,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/integration/api-keys",
    tags=["integration"],
    dependencies=[Depends(require_admin())],
)


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64, description="操作者可读的用途名")


class ApiKeyResource(BaseModel):
    id: int
    name: str
    active: bool
    created_at: datetime
    last_used_at: datetime | None


def _resource(row: ApiKey) -> ApiKeyResource:
    return ApiKeyResource(
        id=row.id,
        name=row.name,
        active=row.active,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Mint an API key")
async def create_api_key(
    body: ApiKeyCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    plaintext, key_hash = generate_api_key()
    row = ApiKey(
        tenant_id=tenant_id,
        name=body.name,
        key_hash=key_hash,
        created_by=user.id,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise APIError(
            f"an API key named {body.name!r} already exists in this tenant",
            code="API_KEY_NAME_TAKEN",
            status_code=status.HTTP_409_CONFLICT,
        ) from exc

    settings = request.app.state.settings
    signing_root = load_signing_root(str(settings.master_key_path), settings.jwt_secret)
    return {
        "key": _resource(row).model_dump(),
        # Shown exactly once. The hash cannot be reversed and the secret is
        # derived, so "we lost it" has one remedy: mint a new key.
        "api_key": plaintext,
        "webhook_secret": derive_webhook_secret(signing_root, row.id),
    }


@router.get("", summary="List API keys")
async def list_api_keys(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    rows = (
        (
            await session.execute(
                select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.id)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_resource(row).model_dump() for row in rows], "total": len(rows)}


@router.post("/{key_id}/revoke", summary="Revoke an API key")
async def revoke_api_key(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = get_tenant_id(request)
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise APIError(
            "API key not found",
            code="API_KEY_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    row.active = False
    await session.commit()
    return {"key": _resource(row).model_dump()}
