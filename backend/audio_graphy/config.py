"""Application configuration.

Loads from environment (or .env file). Exposes a cached `Settings` singleton
via `get_settings()` and an `build_adapters()` factory that picks mock vs real
adapters based on `ADAPTER_MODE`.

Design (per docs/DESIGN.md §15.3):
- All env vars documented in `.env.example`
- `ADAPTER_MODE=mock` (default) returns deterministic mock adapters
- `ADAPTER_MODE=real` would wire to vLLM/funASR/bge-m3 (out of scope for this sprint)
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle

logger = logging.getLogger(__name__)

AdapterMode = Literal["mock", "real"]


class Settings(BaseSettings):
    """Typed application settings loaded from env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Adapter mode ---
    adapter_mode: AdapterMode = "mock"

    # --- Database (MySQL 8) ---
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "audiography"
    mysql_password: str = "change-me"  # noqa: S105 — placeholder, overridden by .env
    mysql_db: str = "audiography"
    mysql_root_password: str = "root"  # noqa: S105 — placeholder, overridden by .env

    # --- LLM (only used when adapter_mode == "real") ---
    openai_base_url_strong: str = "http://vllm-strong:8000/v1"
    openai_base_url_weak: str = "http://vllm-weak:8001/v1"
    openai_api_key: str = "dummy"
    llm_strong_model: str = "qwen3.6-27b"
    llm_weak_model: str = "qwen3.6-35b-a3b"

    # --- ASR / VAD / Embedding (real mode only) ---
    funasr_url: str = "http://funasr:10095"
    silero_vad_url: str = "http://silero-vad:8001"
    bge_m3_url: str = "http://bge-m3:8080"
    embedding_dim: int = 1024

    # --- Storage ---
    working_dir: Path = Path("/data/working_dir")

    # --- Multi-tenancy ---
    default_tenant_id: str = "default"

    # --- Auth (JWT) ---
    jwt_secret: str = Field(default="change-me-to-32-char-random-please", min_length=16)
    jwt_exp_hours: int = 12
    jwt_refresh_exp_hours: int = 84
    jwt_algorithm: str = "HS256"

    # --- Password hashing ---
    bcrypt_rounds: int = 12

    # --- Pipeline (APScheduler) ---
    pipeline_poll_seconds: int = 5
    pipeline_concurrency: int = 1

    # --- Retention ---
    recording_retention_days: int = 90

    # --- Feature flags ---
    enable_clap: bool = False
    enable_voiceprint: bool = False

    # --- Mock flakiness (testing) ---
    mock_asr_flaky: bool = False
    mock_llm_error_rate: float = 0.005

    # --- Logging ---
    log_level: str = "INFO"
    log_format: str = "json"

    # --- CORS ---
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ----------------------------------------------------------
    # Derived properties
    # ----------------------------------------------------------
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def mysql_dsn_async(self) -> str:
        """Async SQLAlchemy DSN (aiomysql)."""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def mysql_dsn_sync(self) -> str:
        """Sync SQLAlchemy DSN (PyMySQL) — used by Alembic."""
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def mysql_dsn_test(self) -> str:
        """DSN for the test database (auto-created by init/01_schema.sql)."""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/audiography_test?charset=utf8mb4"
        )

    # ----------------------------------------------------------
    # Validators
    # ----------------------------------------------------------
    @field_validator("working_dir")
    @classmethod
    def _ensure_working_dir_exists(cls, v: Path) -> Path:
        """working_dir must exist and be writable for VideoRAG file index."""
        v.mkdir(parents=True, exist_ok=True)
        return v.resolve()

    @model_validator(mode="after")
    def _validate_combinations(self) -> Settings:
        """Cross-field validation."""
        if self.adapter_mode == "real" and self.jwt_secret.startswith("change-me"):
            logger.warning(
                "ADAPTER_MODE=real but JWT_SECRET is still the placeholder — "
                "production deployments must override JWT_SECRET."
            )
        if self.mock_llm_error_rate < 0 or self.mock_llm_error_rate > 1:
            raise ValueError("MOCK_LLM_ERROR_RATE must be in [0.0, 1.0]")
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Tests reset the cache via `get_settings.cache_clear()` (see conftest.py).
    """
    return Settings()


def build_adapters(settings: Settings) -> AdapterBundle:
    """Factory that returns the appropriate adapter bundle.

    M1.5: returns mock bundle unconditionally (real impl lands in Phase 2+).
    The Protocol contract is defined in `audio_graphy.adapters.protocols`.

    Args:
        settings: application settings

    Returns:
        AdapterBundle with 4 adapters (asr / strong_llm / weak_llm / embed / vad)
    """
    from audio_graphy.adapters.bundle import build_mock_bundle

    if settings.adapter_mode == "mock":
        logger.info("Building MOCK adapter bundle")
        return build_mock_bundle(settings)

    # Real mode — not implemented in this sprint.
    raise NotImplementedError(
        "ADAPTER_MODE=real is not implemented in M1. "
        "Real adapters land in a follow-up sprint with vLLM/funASR services."
    )
