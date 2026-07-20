"""Auth schemas: login, token, user info.

See: docs/m3-prd.md §4.1.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """POST /auth/login request body."""

    email: EmailStr = Field(description="User email")
    password: str = Field(min_length=1, description="Plain-text password (TLS transport)")


class RefreshRequest(BaseModel):
    """POST /auth/refresh request body."""

    refresh_token: str = Field(description="Valid refresh token")


class UserInfo(BaseModel):
    """User info embedded in token response and /me."""

    id: int
    name: str
    email: str
    role: str
    tenant_id: str


class MeResponse(BaseModel):
    """GET /auth/me response (includes created_at)."""

    id: int
    name: str
    email: str
    role: str
    tenant_id: str
    created_at: datetime | None = None


class TokenResponse(BaseModel):
    """POST /auth/login and POST /auth/refresh response."""

    access_token: str
    refresh_token: str | None = Field(default=None, description="Only returned by /login")
    token_type: str = Field(default="bearer")
    expires_in: int = Field(description="Access token TTL in seconds")
    user: UserInfo | None = Field(default=None, description="Only returned by /login")


class RefreshTokenResponse(BaseModel):
    """POST /auth/refresh response (no refresh_token, no user)."""

    access_token: str
    token_type: str = Field(default="bearer")
    expires_in: int = Field(description="Access token TTL in seconds")
