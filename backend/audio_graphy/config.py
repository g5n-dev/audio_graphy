"""Application configuration.

Loads from environment (or .env file). Exposes a cached `Settings` singleton
via `get_settings()` and a `build_adapters()` factory that picks mock vs real
adapters based on the four per-adapter mode fields (`ADAPTER_{ASR,VAD,LLM,EMBED}_MODE`).

设计说明（docs/DESIGN.md §15.3 + docs/m4-architecture.md §4）：
- All env vars documented in `.env.example`.
- The legacy `ADAPTER_MODE` field is retained only for back-compat with M3 `.env`
  files; it no longer drives mode resolution. Set the 4 per-adapter fields instead.
- ASR real mode is rejected by the validator (funASR lands in M5).
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
    # Legacy global default — retained for M3 back-compat; does NOT drive mode
    # resolution (Q5 locked: per-adapter fields below are the sole source of truth).
    adapter_mode: AdapterMode = "mock"
    # Per-adapter modes (M4). Default "mock" preserves M3 all-mock behavior.
    # Set to "real" to enable the corresponding service via docker-compose `--profile real`.
    adapter_asr_mode: AdapterMode = "mock"   # M5: set to "real" to enable funASR
    adapter_vad_mode: AdapterMode = "mock"
    adapter_llm_mode: AdapterMode = "mock"
    adapter_embed_mode: AdapterMode = "mock"

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
    funasr_url: str = "http://funasr:8000"  # M5: OpenAI-compat endpoint
    funasr_model: str = "fun-asr-nano"
    silero_vad_url: str = "http://silero-vad:8002"  # compose maps 8002:8000
    bge_m3_url: str = "http://bge-m3:8080"
    embedding_dim: int = 1024

    # --- Eval subsystem (M5 — WS-2) ---
    judge_llm_model: str = ""  # empty → fallback to llm_strong_model
    eval_concurrency: int = 4

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

    @property
    def judge_llm_model_resolved(self) -> str:
        """Judge LLM model with fallback to ``llm_strong_model``."""
        return self.judge_llm_model or self.llm_strong_model

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
        """Cross-field validation / 跨字段校验."""
        # M3 back-compat: legacy global field still triggers JWT warning.
        if self.adapter_mode == "real" and self.jwt_secret.startswith("change-me"):
            logger.warning(
                "ADAPTER_MODE=real but JWT_SECRET is placeholder — "
                "this field is retained for compatibility; effective modes are "
                "ADAPTER_{ASR,VAD,LLM,EMBED}_MODE."
            )
        if self.mock_llm_error_rate < 0 or self.mock_llm_error_rate > 1:
            raise ValueError("MOCK_LLM_ERROR_RATE must be in [0.0, 1.0]")

        # M5 — JWT warning if ANY real adapter mode enabled (now including ASR).
        real_modes = [
            self.adapter_asr_mode,
            self.adapter_vad_mode,
            self.adapter_llm_mode,
            self.adapter_embed_mode,
        ]
        if "real" in real_modes and self.jwt_secret.startswith("change-me"):
            logger.warning(
                "REAL adapter ON but JWT_SECRET is placeholder — set a strong JWT_SECRET"
            )

        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Tests reset the cache via `get_settings.cache_clear()` (see conftest.py).
    """
    return Settings()


def build_adapters(settings: Settings) -> AdapterBundle:
    """Factory that returns the appropriate adapter bundle.

    工厂函数：依据 4 个 per-adapter mode 字段选择 bundle。

    Resolution rule (Q5 locked, docs/m4-architecture.md §1.6):
    - Per-adapter fields (`adapter_asr_mode` / `_vad_mode` / `_llm_mode` / `_embed_mode`)
      are the SOLE source of truth.
    - Legacy `adapter_mode` is consulted only for the JWT warning in the validator.
    - If all 4 are "mock" → ``build_mock_bundle`` (fast path, M3 behavior preserved).
    - Else → ``build_hybrid_bundle``.

    Args:
        settings: application settings

    Returns:
        AdapterBundle with 5 adapters (vad / asr / strong_llm / weak_llm / embed)
    """
    from audio_graphy.adapters.bundle import build_hybrid_bundle, build_mock_bundle

    all_mock = all(m == "mock" for m in (
        settings.adapter_asr_mode,
        settings.adapter_vad_mode,
        settings.adapter_llm_mode,
        settings.adapter_embed_mode,
    ))
    if all_mock:
        logger.info("Building MOCK adapter bundle (all-mock)")
        return build_mock_bundle(settings)
    logger.info(
        "Building HYBRID adapter bundle (asr=%s vad=%s llm=%s embed=%s)",
        settings.adapter_asr_mode, settings.adapter_vad_mode,
        settings.adapter_llm_mode, settings.adapter_embed_mode,
    )
    return build_hybrid_bundle(settings)
