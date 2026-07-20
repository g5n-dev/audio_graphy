"""Auth router — login, refresh, me.

See: docs/m3-prd.md §4.1, API-01.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_jwt_manager
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.config import Settings, get_settings
from audio_graphy.errors import InvalidCredentialsError, InvalidRefreshTokenError
from audio_graphy.models.user import User
from audio_graphy.schemas.auth import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RefreshTokenResponse,
    TokenResponse,
    UserInfo,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, summary="User login")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    jwt_manager: JWTManager = Depends(get_jwt_manager),
) -> TokenResponse:
    """Authenticate a user and return JWT tokens.

    In mock adapter mode, password verification is skipped.
    In real mode, bcrypt verification is enforced.
    """
    from audio_graphy.auth.passwords import PasswordHasher

    # Look up user by email (no tenant filter — email identifies the user)
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalars().first()

    if user is None:
        raise InvalidCredentialsError(
            detail={"email": body.email},
        )

    # Password verification
    hasher = PasswordHasher(
        adapter_mode=settings.adapter_mode,
        bcrypt_rounds=settings.bcrypt_rounds,
    )
    if not hasher.verify(body.password, user.password_hash):
        raise InvalidCredentialsError(
            detail={"email": body.email},
        )

    # Generate tokens
    tenant_id = str(user.tenant_id)
    role = str(user.role)
    access_token = jwt_manager.create_access_token(user.id, tenant_id, role)
    refresh_token = jwt_manager.create_refresh_token(user.id, tenant_id, role)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=jwt_manager.expires_in,
        user=UserInfo(
            id=user.id,
            name=str(user.name),
            email=str(user.email),
            role=role,
            tenant_id=tenant_id,
        ),
    )


@router.post("/refresh", response_model=RefreshTokenResponse, summary="Refresh access token")
async def refresh(
    body: RefreshRequest,
    jwt_manager: JWTManager = Depends(get_jwt_manager),
) -> RefreshTokenResponse:
    """Exchange a refresh token for a new access token."""
    try:
        payload = jwt_manager.verify_refresh_token(body.refresh_token)
    except InvalidRefreshTokenError:
        raise

    # Issue new access token
    access_token = jwt_manager.create_access_token(payload.sub, payload.tid, payload.role)

    return RefreshTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=jwt_manager.expires_in,
    )


@router.get("/me", response_model=MeResponse, summary="Get current user")
async def me(
    current_user: AuthUser = Depends(get_current_user),
) -> MeResponse:
    """Return the current authenticated user's info."""
    return MeResponse(
        id=current_user.id,
        name=current_user.name,
        email=current_user.email,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
    )
