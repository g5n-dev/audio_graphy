"""Application configuration.

Loads from environment (or .env file). Exposes a cached `Settings` singleton
via `get_settings()` and a `build_adapters()` factory that picks mock vs real
adapters based on the four per-adapter mode fields (`ADAPTER_{ASR,VAD,LLM,EMBED}_MODE`).

设计说明（docs/DESIGN.md §15.3 + docs/m4-architecture.md §4）：
- All env vars documented in `.env.example`.
- The legacy `ADAPTER_MODE` field is retained only for back-compat with M3 `.env`
  files; it no longer drives mode resolution. Set the 4 per-adapter fields instead.
- No validator gates ASR real mode. It was gated before M5; the gate is gone and
  `ADAPTER_ASR_MODE=real` builds a FunASRAdapter (tests/config/test_settings.py
  asserts this). Enabling any real adapter only warns about a placeholder
  JWT_SECRET. Said here because the previous wording claimed a rejecting
  validator that does not exist, and sent people looking for it.
"""

from __future__ import annotations

import functools
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle

logger = logging.getLogger(__name__)

AdapterMode = Literal["mock", "real"]
LLMRecipeMigrationMode = Literal["shadow", "dual_read", "v2"]
StructuredOutputCapability = Literal[
    "strict_json_schema",
    "json_object",
    "unsupported",
]


class Settings(BaseSettings):
    """Typed application settings loaded from env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Deployment identity ---
    # Which stack this process belongs to, for resources scoped to a SERVER
    # rather than a schema: MySQL GET_LOCK advisory locks are server-global, so
    # two deployments sharing one MySQL would contend on (and time out against)
    # each other's locks unless the name carries this. Defaulted, never
    # required (a required field would fail validation in every worker the
    # moment this file loads); operators running a second stack set it in that
    # stack's .env alongside COMPOSE_RESOURCE_PREFIX. Deliberately NOT derived
    # from the database name — compose pins MYSQL_DATABASE identically for
    # every stack, so that would compute the same value for both.
    deployment_id: str = "audiography"

    # --- Adapter mode ---
    # Legacy global default — retained for M3 back-compat; does NOT drive mode
    # resolution (Q5 locked: per-adapter fields below are the sole source of truth).
    adapter_mode: AdapterMode = "mock"
    # Per-adapter modes (M4). Default "mock" preserves M3 all-mock behavior.
    # Set to "real" to enable the corresponding service via docker-compose `--profile real`.
    adapter_asr_mode: AdapterMode = "mock"  # M5: set to "real" to enable funASR
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
    # Both vLLM containers listen on 8000 internally; only the published host
    # ports differ. Defaulting this to 8001 pointed at nothing, and was masked
    # by .env.example carrying the correct value.
    openai_base_url_weak: str = "http://vllm-weak:8000/v1"
    openai_api_key: str = "dummy"
    llm_strong_model: str = "qwen3.6-27b"
    llm_weak_model: str = "qwen3.6-35b-a3b"
    # Operational model epochs invalidate result recipes even when a served
    # model name is reused for new weights. Empty means "same as model".
    llm_strong_model_epoch: str = ""
    llm_weak_model_epoch: str = ""
    # Declared independently because strong/weak endpoints may run different
    # engines or versions. Unknown values fail Settings validation at startup.
    llm_strong_structured_output_capability: StructuredOutputCapability = "strict_json_schema"
    llm_weak_structured_output_capability: StructuredOutputCapability = "strict_json_schema"
    # Immutable provider price-card version and per-tier rates. Rates use
    # micro-currency-units per one million tokens. The snapshot is either
    # completely absent (all defaults) or complete; partial pricing is rejected.
    llm_price_version: str = ""
    llm_strong_input_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )
    llm_strong_output_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )
    llm_strong_cached_prefill_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )
    llm_weak_input_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )
    llm_weak_output_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )
    llm_weak_cached_prefill_microunits_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
    )

    # --- LLM multi-level cache ---
    llm_hot_cache_backend: Literal["auto", "redis", "local"] = "auto"
    redis_url: SecretStr | None = None
    llm_local_cache_max_entries: int = 1024
    llm_local_cache_max_bytes: int = 32 * 1024 * 1024
    llm_hot_cache_max_item_bytes: int = 1024 * 1024
    llm_local_cache_ttl_seconds: int = 300
    llm_redis_cache_ttl_seconds: int = 3600
    llm_redis_failure_threshold: int = 3
    llm_redis_circuit_seconds: float = 30.0
    llm_redis_recovery_successes: int = 2
    llm_redis_probe_seconds: float = 5.0
    llm_cache_lease_seconds: float = 120.0
    llm_cache_cleanup_interval_seconds: int = 3600
    llm_cache_cleanup_batch_size: int = 500
    llm_cache_max_entries_per_tenant: int = 50_000
    llm_cache_max_bytes_per_tenant: int = 256 * 1024 * 1024
    llm_cache_max_payload_bytes: int = 16 * 1024 * 1024
    llm_recipe_migration_mode: LLMRecipeMigrationMode = "dual_read"
    # Deprecated compatibility switch. When the new field was not explicitly
    # supplied, True maps to shadow and False maps to dual_read.
    llm_recipe_shadow_mode: bool | None = None
    enable_llm_exact_cache: bool = True
    enable_llm_hot_cache: bool = True
    enable_llm_persistent_cache: bool = True
    enable_llm_semantic_cache: bool = False
    enable_llm_batch_judge: bool = False
    enable_hybrid_rule_short_circuit: bool = True
    enable_adaptive_gleaning: bool = False
    llm_strong_concurrency: int = 4
    llm_weak_concurrency: int = 8

    # --- ASR / VAD / Embedding (real mode only) ---
    funasr_url: str = "http://funasr:8000"  # M5: OpenAI-compat endpoint
    funasr_model: str = "fun-asr-nano"
    silero_vad_url: str = "http://silero-vad:8002"  # compose maps 8002:8000
    bge_m3_url: str = "http://bge-m3:8080"
    embedding_dim: int = 1024

    # --- Eval subsystem (M5 — WS-2) ---
    judge_llm_model: str = ""  # empty → fallback to llm_strong_model
    eval_concurrency: int = 4
    # M6: position de-bias toggle (run judge twice on orig + reversed context).
    eval_position_debias: bool = True

    # --- Storage ---
    working_dir: Path = Path("/data/working_dir")
    vector_index_cache_ttl_seconds: float = 60.0
    vector_index_cache_max_entries: int = 32
    vector_index_cache_max_bytes: int = 512 * 1024 * 1024
    vector_index_load_batch_rows: int = 512
    vector_index_load_max_rows: int = 100_000
    vector_index_load_max_source_bytes: int = 512 * 1024 * 1024
    vector_index_load_max_memory_bytes: int = 512 * 1024 * 1024
    graph_store_cache_max_entries: int = 64
    # Maximum induced edges serialized by graph explore/subgraph responses.
    # Requests may lower this budget but can never exceed the absolute 5k cap.
    graph_edge_render_budget: int = Field(default=5_000, ge=1, le=5_000)

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

    # --- PIPL §14.3 (M6) — audio encryption ---
    master_key_path: str = "/run/secrets/audiography_master.key"
    max_recording_audio_bytes: int = 512 * 1024 * 1024
    audio_crypto_chunk_size_bytes: int = 4 * 1024 * 1024
    max_request_body_bytes: int = 16 * 1024 * 1024

    # --- Physical reception audio assembly ---
    audio_assembly_max_sources: int = 128
    audio_assembly_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    audio_assembly_max_estimated_pcm_bytes: int = 2 * 1024 * 1024 * 1024
    audio_assembly_max_temporary_bytes: int = 2 * 1024 * 1024 * 1024
    audio_assembly_ffprobe_timeout_sec: float = 30.0
    audio_assembly_ffmpeg_timeout_sec: float = 15 * 60.0
    audio_assembly_max_processes: int = 2

    # --- Entity fuzzy matching (M6 WS-3) ---
    # rapidfuzz fuzz.WRatio threshold for clustering near-duplicate entity
    # names. 0.85 (default) catches ``CS75 Plus`` ↔ ``CS75PLUS`` and similar
    # Chinese variants without over-merging. Range: 0.80 (loose, max recall)
    # to 0.90 (strict, max precision).
    entity_fuzzy_threshold: float = 0.85

    # --- M7 Phase 2 — adapter modes (CLAP audio embed + CAM++ voiceprint) ---
    adapter_audio_embed_mode: AdapterMode = "mock"
    adapter_voiceprint_mode: AdapterMode = "mock"

    # --- M7 Phase 2 — service URLs ---
    clap_service_url: str = "http://clap-service:8006"
    campplus_service_url: str = "http://campplus-service:8007"

    # --- M7 Phase 2 — speaker linker thresholds (L9 / Q2) ---
    # voiceprint_cosine_threshold: minimum cosine for cross-recording merge
    # via Layer-1 voiceprint matching (SpeakerLinker). Below this, speakers
    # are NOT merged by voiceprint. Range [0.0, 1.0].
    voiceprint_cosine_threshold: float = 0.5
    # voiceprint_ambiguous_threshold: cosine ≥ this → unambiguous merge
    # (ambiguity_tag=None). Below this but ≥ voiceprint_cosine_threshold →
    # merge with ambiguity_tag="AMBIGUOUS". Range [0.0, 1.0].
    voiceprint_ambiguous_threshold: float = 0.7

    # --- Voiceprint sampling (ADR-0001) ---
    # How a per-recording candidate voiceprint is built from the diarization
    # timeline. "weighted_mean" (default) extracts one embedding per qualifying
    # segment and averages them by duration; "longest_segment" extracts a
    # single embedding from the speaker's longest segment (cheaper, but one
    # mis-attributed segment corrupts the whole candidate).
    # Merged reception audio is never a valid input — see ADR-0001.
    voiceprint_sampling_strategy: Literal["weighted_mean", "longest_segment"] = "weighted_mean"
    # Segments shorter than this never contribute to a candidate: sub-second
    # embeddings are dominated by noise and drag same-speaker cosine below
    # voiceprint_cosine_threshold.
    voiceprint_sample_min_segment_sec: float = 1.0
    # A speaker whose qualifying speech totals less than this gets no
    # cross-recording voiceprint at all (too unreliable to merge on).
    voiceprint_sample_min_total_sec: float = 3.0
    # Cost cap: at most this many extract calls per speaker per recording,
    # longest segments first.
    voiceprint_sample_max_segments: int = 8
    # weighted_mean only — segments whose cosine against the first-pass
    # centroid falls below this are dropped as mis-attributed and the
    # centroid is recomputed. 0.0 disables outlier rejection.
    voiceprint_sample_outlier_cosine: float = 0.5

    # --- M7 Phase 2 — GPU strategy flags ---
    clap_force_gpu: bool = True  # L8 enforced at clap-service startup
    campplus_prefer_gpu: bool = False

    # --- M7 Phase 2 — PIPL cascade ---
    # When True (default), DSAR erasure + retention sweep cascade-delete
    # voiceprint_vectors / speaker_nodes / speaker_links for the recording.
    # Disabling is for emergency forensic hold only.
    voiceprint_retention_cascade: bool = True

    # --- M7 Phase 2 — three-channel rerank weights (Q1 locked) ---
    # Order: (text, graph, audio). Sum must be ~1.0 (validator enforces).
    # M7 WS-3 implements rerank; field is reserved here.
    rerank_channel_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)

    # --- Feature flags ---
    enable_clap: bool = False
    # Off by default, and it must stay that way while the adapter mode defaults
    # to mock. Enabling this only checks that *a* voiceprint adapter exists, and
    # MockVoiceprintAdapter satisfies that check — it derives vectors from
    # sha512(speaker_id), so the same diarization label in two unrelated
    # recordings lands at cosine ~0.83, above voiceprint_ambiguous_threshold.
    # Speaker linking then merges strangers into one identity without raising
    # AMBIGUOUS or queueing a review, and persists the result as encrypted
    # biometric data. Turn this on together with ADAPTER_VOICEPRINT_MODE=real;
    # the validator below warns about the other combination.
    enable_voiceprint: bool = False

    # --- Startup strictness ---
    # Serve even when the database engine could not be created. Off by default:
    # an application answering requests with no database is worse than one that
    # refuses to start, because the failure surfaces later and further away.
    # The test suite enables it so the API tests can run against a stub.
    allow_degraded_startup: bool = False

    # --- M8 Phase 4 — streaming feature flags ---
    # Master switch. When False (default), /ws/stream route is NOT registered
    # and M1-M7 tests have zero regression (PRD AC-P0-07 + §17.11).
    enable_streaming: bool = False
    # Streaming retrieval (Q3 默认权重 0.5). Off by default — WS-3 will flip.
    enable_streaming_retrieval: bool = False

    # --- M8 Phase 4 — streaming adapter modes ---
    adapter_streaming_vad_mode: AdapterMode = "mock"
    adapter_streaming_asr_mode: AdapterMode = "mock"

    # --- M8 Phase 4 — service endpoints ---
    funasr_ws_url: str = "ws://funasr:10095"
    silero_vad_model_path: str = "/models/silero_vad.onnx"

    # --- M8 Phase 4 — Silero thresholds (L3 locked defaults) ---
    streaming_vad_onset_threshold: float = 0.5
    streaming_vad_offset_threshold: float = 0.35
    streaming_vad_min_speech_sec: float = 0.25
    streaming_vad_min_silence_sec: float = 0.10
    streaming_vad_chunk_samples: int = 512  # L3 — do NOT change
    streaming_vad_reset_seq_gap: int = 3  # Q2 — reset threshold

    # --- M8 Phase 4 — funASR streaming config ---
    streaming_asr_chunk_interval: int = 10
    streaming_asr_connect_timeout_sec: float = 5.0
    streaming_asr_push_timeout_sec: float = 30.0
    streaming_asr_finalize_timeout_sec: float = 5.0
    streaming_asr_pool_size_per_tenant: int = 8  # Q1

    # --- M8 Phase 4 — session & WS lifecycle ---
    streaming_tag_interval: int = 5  # L7
    streaming_tag_debounce_ms: float = 500.0
    streaming_session_timeout_sec: float = 300.0  # PRD §5.3
    streaming_session_pcm_buffer_max_sec: float = 60.0  # PIPL cap
    ws_heartbeat_interval_sec: float = 30.0
    ws_max_recv_queue: int = 200
    ws_backpressure_warn: int = 100
    streaming_ws_ticket_ttl_sec: int = 60
    # Emergency rollout compatibility only. Production defaults to one-time
    # tickets so long-lived bearer credentials never appear in WS URLs.
    streaming_allow_legacy_jwt_query: bool = False

    # --- M8 Phase 4 — AMBIGUOUS edge downweight (Q3) ---
    streaming_ambiguous_edge_weight: float = 0.5
    streaming_inferred_edge_weight: float = 0.8

    # --- M8 Phase 4 — JWT TTL for WS (shorter than REST) ---
    ws_jwt_ttl_minutes: int = 5  # PRD §5.3

    # --- M9 R1 T15 — advanced graph feature flags (L9 zero-regression) ---
    # Master switch. When False, every M9 subsystem degrades to its M8
    # baseline (no bi-temporal writes, no Leiden, no compression, no Layer 2
    # fuzzy). Defaults to False per L9 ruling.
    enable_advanced_graph: bool = False
    # Bi-temporal timestamps + supersede pointer (architecture §6).
    # When False, GraphEdge is constructed without bi-temporal fields.
    enable_bitemporal_edges: bool = True
    # Leiden / HIT-Leiden incremental community detection (architecture §7).
    enable_leiden: bool = True
    # L2 — diff percentage cap above which an incremental run becomes a full
    # recompute. Default 30% (locked L2).
    leiden_threshold_percent: float = 30.0
    # Preferred Leiden backend: ``"leidenalg"`` (best, optional dep),
    # ``"networkx"`` (always-available fallback), ``"fail-fast"``
    # (raise if leidenalg is missing).
    leiden_lib: Literal["leidenalg", "networkx", "fail-fast"] = "networkx"
    # Maximum Leiden hierarchy levels actually persisted (Q2 cap=2; level 3
    # dropped). Must be in [0, 3].
    leiden_max_levels: int = 2

    # Community summaries (architecture §8 / Q2).
    # Strategy: ``eager`` = generate level-0 + leaf at Leiden time;
    # ``lazy``   = generate levels 1-2 only on retrieval request;
    # ``disabled`` = do not generate summaries at all.
    community_summary_strategy: Literal["eager", "lazy", "disabled"] = "eager"

    # Compression (architecture §9 / Q3 SOFT-only).
    enable_compression: bool = True
    # Compression candidate scoring thresholds (architecture §9.1).
    compression_god_node_degree: int = 50
    compression_stale_days: int = 180
    # Per-run cap on candidates (prevents runaway compression batches).
    compression_max_candidates_per_run: int = 100
    # Report what the weekly sweep would change without writing. Useful for the
    # first run against tenants it never reached before: the sweep resolved its
    # tenant list from an attribute Settings never defined, so until that was
    # fixed it only ever swept "default".
    compression_dry_run: bool = False
    # L6 locked — low-degree node merge: nodes with degree ≤ this AND
    # rapidfuzz token_ratio ≥ compression_fuzzy_token_ratio (same community)
    # become merge candidates. Default 1 (architecture §9.2 / §9.3).
    compression_degree_threshold: int = 1
    # L6 locked – rapidfuzz fuzz.token_ratio threshold (× 100) for the
    # low-degree merge decision. Default 85 (architecture §9.3).
    compression_fuzzy_token_ratio: int = 85
    # L7 locked – AMBIGUOUS edges older than this many days without a
    # re-encounter event get demoted to confidence='DEPRECATED'.
    # Default 30 (architecture §9.4).
    compression_ambiguous_deprecate_days: int = 30

    # SpeakerLinker L8 — fuzzy thresholds (binding).
    speaker_fuzzy_ambiguous_threshold: float = 0.85
    speaker_fuzzy_inferred_threshold: float = 0.6
    speaker_fuzzy_voiceprint_reconfirm_cosine: float = 0.7
    # When True, SpeakerLinker invokes SpeakerFuzzyMatcher on Layer-1 misses.
    # Default True (L8 active). Flip to False to recover M7 behaviour exactly.
    enable_speaker_layer2_fuzzy: bool = True

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

    @property
    def llm_recipe_migration_mode_resolved(self) -> LLMRecipeMigrationMode:
        """Resolve the new three-state recipe rollout with legacy compatibility."""

        if "llm_recipe_migration_mode" in self.model_fields_set:
            return self.llm_recipe_migration_mode
        if self.llm_recipe_shadow_mode is not None:
            return "shadow" if self.llm_recipe_shadow_mode else "dual_read"
        return self.llm_recipe_migration_mode

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

    @field_validator(
        "vector_index_cache_max_entries",
        "vector_index_cache_max_bytes",
        "vector_index_load_batch_rows",
        "vector_index_load_max_rows",
        "vector_index_load_max_source_bytes",
        "vector_index_load_max_memory_bytes",
        "graph_store_cache_max_entries",
        "llm_local_cache_max_entries",
        "llm_local_cache_max_bytes",
        "llm_hot_cache_max_item_bytes",
        "llm_local_cache_ttl_seconds",
        "llm_redis_cache_ttl_seconds",
        "llm_redis_failure_threshold",
        "llm_redis_recovery_successes",
        "llm_cache_cleanup_interval_seconds",
        "llm_cache_cleanup_batch_size",
        "llm_cache_max_entries_per_tenant",
        "llm_cache_max_bytes_per_tenant",
        "llm_cache_max_payload_bytes",
        "llm_strong_concurrency",
        "llm_weak_concurrency",
    )
    @classmethod
    def _validate_positive_cache_resource_integer(cls, v: int) -> int:
        if v < 1:
            raise ValueError("cache and vector load resource limits must be positive")
        return v

    @field_validator("vector_index_cache_ttl_seconds")
    @classmethod
    def _validate_vector_cache_ttl(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("VECTOR_INDEX_CACHE_TTL_SECONDS must be finite and non-negative")
        return v

    @field_validator("redis_url", mode="before")
    @classmethod
    def _normalize_redis_url(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "llm_redis_circuit_seconds",
        "llm_redis_probe_seconds",
        "llm_cache_lease_seconds",
    )
    @classmethod
    def _validate_positive_llm_cache_duration(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("LLM cache durations must be finite and positive")
        return v

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

        price_rates = (
            self.llm_strong_input_microunits_per_million_tokens,
            self.llm_strong_output_microunits_per_million_tokens,
            self.llm_strong_cached_prefill_microunits_per_million_tokens,
            self.llm_weak_input_microunits_per_million_tokens,
            self.llm_weak_output_microunits_per_million_tokens,
            self.llm_weak_cached_prefill_microunits_per_million_tokens,
        )
        configured_price_rates = sum(rate is not None for rate in price_rates)
        price_version = self.llm_price_version.strip()
        if configured_price_rates not in {0, len(price_rates)}:
            raise ValueError("LLM price snapshot rates must be configured all-or-none")
        if bool(price_version) != bool(configured_price_rates):
            raise ValueError(
                "LLM_PRICE_VERSION and all LLM tier price rates must be configured together"
            )
        if configured_price_rates:
            strong_input = self.llm_strong_input_microunits_per_million_tokens
            strong_cached = self.llm_strong_cached_prefill_microunits_per_million_tokens
            weak_input = self.llm_weak_input_microunits_per_million_tokens
            weak_cached = self.llm_weak_cached_prefill_microunits_per_million_tokens
            assert strong_input is not None and strong_cached is not None
            assert weak_input is not None and weak_cached is not None
            if strong_cached > strong_input or weak_cached > weak_input:
                raise ValueError("LLM cached-prefill rate cannot exceed the regular input rate")
            self.llm_price_version = price_version

        if self.redis_url is not None:
            from urllib.parse import urlsplit

            redis_url = self.redis_url.get_secret_value()
            if urlsplit(redis_url).scheme not in {"redis", "rediss", "unix"}:
                raise ValueError("REDIS_URL must use redis://, rediss://, or unix://")
        if self.llm_hot_cache_backend == "redis" and self.redis_url is None:
            raise ValueError("REDIS_URL is required when LLM_HOT_CACHE_BACKEND=redis")
        if self.llm_hot_cache_max_item_bytes > self.llm_local_cache_max_bytes:
            raise ValueError("LLM hot-cache item limit cannot exceed the local cache byte limit")
        if self.llm_local_cache_ttl_seconds > 300:
            raise ValueError("LLM_LOCAL_CACHE_TTL_SECONDS cannot exceed 300")
        if self.llm_redis_cache_ttl_seconds > 3600:
            raise ValueError("LLM_REDIS_CACHE_TTL_SECONDS cannot exceed 3600")

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

        # M7 — speaker linker thresholds sanity (no crossover).
        if self.voiceprint_cosine_threshold > self.voiceprint_ambiguous_threshold:
            raise ValueError(
                "VOICEPRINT_COSINE_THRESHOLD must be ≤ "
                f"VOICEPRINT_AMBIGUOUS_THRESHOLD (got {self.voiceprint_cosine_threshold} "
                f"> {self.voiceprint_ambiguous_threshold})"
            )

        # ADR-0001 — voiceprint sampling gates must be internally consistent.
        if self.voiceprint_sample_min_segment_sec > self.voiceprint_sample_min_total_sec:
            raise ValueError(
                "VOICEPRINT_SAMPLE_MIN_SEGMENT_SEC must be ≤ "
                f"VOICEPRINT_SAMPLE_MIN_TOTAL_SEC (got "
                f"{self.voiceprint_sample_min_segment_sec} > "
                f"{self.voiceprint_sample_min_total_sec}) — otherwise no speaker "
                "can ever reach the total-speech gate"
            )

        # M9 R1 T15 — advanced-graph sanity checks.
        # Leiden levels cap (Q2 cap=2 in practice; allow 0..3 for forward-compat).
        if not 0 <= self.leiden_max_levels <= 3:
            raise ValueError(f"LEIDEN_MAX_LEVELS must be in [0, 3], got {self.leiden_max_levels}")
        # L8 — inferred ≤ ambiguous.
        if self.speaker_fuzzy_inferred_threshold > self.speaker_fuzzy_ambiguous_threshold:
            raise ValueError(
                "SPEAKER_FUZZY_INFERRED_THRESHOLD must be ≤ "
                f"SPEAKER_FUZZY_AMBIGUOUS_THRESHOLD (got "
                f"{self.speaker_fuzzy_inferred_threshold} > "
                f"{self.speaker_fuzzy_ambiguous_threshold})"
            )
        # M9 sub-flags require the master flag.
        if not self.enable_advanced_graph:
            sub_flags_on = []
            if self.enable_bitemporal_edges:
                sub_flags_on.append("ENABLE_BITEMPORAL_EDGES")
            if self.enable_leiden:
                sub_flags_on.append("ENABLE_LEIDEN")
            if self.enable_compression:
                sub_flags_on.append("ENABLE_COMPRESSION")
            if sub_flags_on:
                logger.warning(
                    "ENABLE_ADVANCED_GRAPH=False but sub-flags ON: %s. "
                    "Sub-flags are ignored when the master flag is False "
                    "(L9 zero-regression).",
                    ", ".join(sub_flags_on),
                )

        # Speaker linking on top of fabricated voiceprints. Not an error: the
        # combination is what the mock-chain tests exercise deliberately. But it
        # produces confident nonsense rather than degraded output — mock vectors
        # are derived from the diarization label, so the same label in unrelated
        # recordings matches above the unambiguous-merge threshold and strangers
        # are silently linked into one speaker identity.
        if self.enable_voiceprint and self.adapter_voiceprint_mode != "real":
            logger.warning(
                "ENABLE_VOICEPRINT=true with ADAPTER_VOICEPRINT_MODE=%s: speaker "
                "linking will run on mock voiceprints, which match across "
                "unrelated recordings and merge distinct speakers without review. "
                "Set ADAPTER_VOICEPRINT_MODE=real before trusting any speaker "
                "identity this produces.",
                self.adapter_voiceprint_mode,
            )

        return self

    # ----------------------------------------------------------
    # M7 Phase 2 — per-field validators
    # ----------------------------------------------------------
    @field_validator("voiceprint_cosine_threshold")
    @classmethod
    def _validate_vp_cosine_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"VOICEPRINT_COSINE_THRESHOLD must be in [0, 1], got {v}")
        return v

    @field_validator(
        "voiceprint_sample_min_segment_sec",
        "voiceprint_sample_min_total_sec",
    )
    @classmethod
    def _validate_vp_sample_durations(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError(f"voiceprint sampling duration must be > 0, got {v}")
        return v

    @field_validator("voiceprint_sample_max_segments")
    @classmethod
    def _validate_vp_sample_max_segments(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"VOICEPRINT_SAMPLE_MAX_SEGMENTS must be ≥ 1, got {v}")
        return v

    @field_validator("voiceprint_sample_outlier_cosine")
    @classmethod
    def _validate_vp_sample_outlier_cosine(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"VOICEPRINT_SAMPLE_OUTLIER_COSINE must be in [0, 1], got {v}")
        return v

    @field_validator(
        "max_recording_audio_bytes",
        "audio_crypto_chunk_size_bytes",
        "max_request_body_bytes",
        "audio_assembly_max_sources",
        "audio_assembly_max_total_bytes",
        "audio_assembly_max_estimated_pcm_bytes",
        "audio_assembly_max_temporary_bytes",
        "audio_assembly_max_processes",
    )
    @classmethod
    def _validate_positive_audio_resource_integer(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("audio resource limits must be positive")
        return v

    @field_validator("audio_crypto_chunk_size_bytes")
    @classmethod
    def _validate_audio_crypto_chunk_size(cls, v: int) -> int:
        if not 1024 <= v <= 16 * 1024 * 1024:
            raise ValueError("AUDIO_CRYPTO_CHUNK_SIZE_BYTES must be in [1024, 16777216]")
        return v

    @field_validator(
        "audio_assembly_ffprobe_timeout_sec",
        "audio_assembly_ffmpeg_timeout_sec",
    )
    @classmethod
    def _validate_positive_audio_timeout(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("audio process timeouts must be positive")
        return v

    @field_validator("voiceprint_ambiguous_threshold")
    @classmethod
    def _validate_vp_ambiguous_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"VOICEPRINT_AMBIGUOUS_THRESHOLD must be in [0, 1], got {v}")
        return v

    # ----------------------------------------------------------
    # M9 R1 T15 — per-field validators
    # ----------------------------------------------------------
    @field_validator("leiden_threshold_percent")
    @classmethod
    def _validate_leiden_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 100.0:
            raise ValueError(f"LEIDEN_THRESHOLD_PERCENT must be in [0, 100], got {v}")
        return v

    @field_validator("speaker_fuzzy_ambiguous_threshold")
    @classmethod
    def _validate_speaker_fuzzy_ambiguous(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"SPEAKER_FUZZY_AMBIGUOUS_THRESHOLD must be in [0, 1], got {v}")
        return v

    @field_validator("speaker_fuzzy_inferred_threshold")
    @classmethod
    def _validate_speaker_fuzzy_inferred(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"SPEAKER_FUZZY_INFERRED_THRESHOLD must be in [0, 1], got {v}")
        return v

    @field_validator("speaker_fuzzy_voiceprint_reconfirm_cosine")
    @classmethod
    def _validate_speaker_fuzzy_vp_cosine(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"SPEAKER_FUZZY_VOICEPRINT_RECONFIRM_COSINE must be in [0, 1], got {v}"
            )
        return v

    @field_validator("rerank_channel_weights")
    @classmethod
    def _validate_rerank_weights(cls, v: tuple[float, float, float]) -> tuple[float, float, float]:
        total = sum(v)
        if not 0.99 <= total <= 1.01:
            raise ValueError(f"RERANK_CHANNEL_WEIGHTS must sum to 1.0, got {v} (sum={total})")
        if not all(0.0 <= x <= 1.0 for x in v):
            raise ValueError(f"All weights must be in [0, 1], got {v}")
        return v

    # ----------------------------------------------------------
    # M8 Phase 4 — per-field validators
    # ----------------------------------------------------------
    @field_validator("streaming_vad_onset_threshold")
    @classmethod
    def _validate_streaming_vad_onset(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"STREAMING_VAD_ONSET_THRESHOLD must be in [0, 1], got {v}")
        return v

    @field_validator("streaming_vad_offset_threshold")
    @classmethod
    def _validate_streaming_vad_offset(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"STREAMING_VAD_OFFSET_THRESHOLD must be in [0, 1], got {v}")
        return v

    @field_validator("streaming_ambiguous_edge_weight")
    @classmethod
    def _validate_streaming_ambiguous_w(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"STREAMING_AMBIGUOUS_EDGE_WEIGHT must be in [0, 1], got {v}")
        return v

    @field_validator("streaming_inferred_edge_weight")
    @classmethod
    def _validate_streaming_inferred_w(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"STREAMING_INFERRED_EDGE_WEIGHT must be in [0, 1], got {v}")
        return v

    @field_validator("streaming_vad_reset_seq_gap")
    @classmethod
    def _validate_streaming_reset_gap(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"STREAMING_VAD_RESET_SEQ_GAP must be ≥ 1, got {v}")
        return v

    @field_validator("streaming_asr_pool_size_per_tenant")
    @classmethod
    def _validate_pool_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"STREAMING_ASR_POOL_SIZE_PER_TENANT must be ≥ 1, got {v}")
        return v


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

    all_mock = all(
        m == "mock"
        for m in (
            settings.adapter_asr_mode,
            settings.adapter_vad_mode,
            settings.adapter_llm_mode,
            settings.adapter_embed_mode,
        )
    )
    if all_mock:
        logger.info("Building MOCK adapter bundle (all-mock)")
        return build_mock_bundle(settings)
    logger.info(
        "Building HYBRID adapter bundle (asr=%s vad=%s llm=%s embed=%s)",
        settings.adapter_asr_mode,
        settings.adapter_vad_mode,
        settings.adapter_llm_mode,
        settings.adapter_embed_mode,
    )
    return build_hybrid_bundle(settings)
