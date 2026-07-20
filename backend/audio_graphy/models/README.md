# AudioGraphy ORM Models

SQLAlchemy 2.0 ORM models for the AudioGraphy store-recording graph retrieval
and multi-level tagging system.

## Table Overview

13 core tables organized in two groups:

### Global Tables (inherit `Base`)

| Model     | Table         | Description                          |
| --------- | ------------- | ------------------------------------ |
| `Tenant`  | `tenants`     | Multi-tenant root entity             |
| `Prompt`  | `prompts`     | LLM prompt version management        |

### Tenant-Scoped Business Tables (inherit `TenantScopedBase`)

| Model          | Table            | Description                              |
| -------------- | ---------------- | ---------------------------------------- |
| `User`         | `users`          | RBAC users                               |
| `Recording`    | `recordings`     | Recording pipeline master                |
| `Segment`      | `segments`       | VAD-split audio segments                 |
| `Chunk`        | `chunks`         | Text chunks for extraction/embedding     |
| `TagFact`      | `tag_facts`      | Append-only tag versioning (Layer 1)     |
| `TagCurrent`   | `tag_current`    | Current effective tags (Layer 2)         |
| `TagStat`      | `tag_stats`      | Tag statistics aggregation (Layer 3)     |
| `VectorEntity` | `vectors_entity` | Entity embeddings for cosine search      |
| `VectorChunk`  | `vectors_chunk`  | Chunk embeddings for cosine search       |
| `AuditLog`     | `audit_logs`     | Sensitive operation audit trail          |
| `LLMCallLog`   | `llm_call_logs`  | LLM API call instrumentation             |

## Inheritance Hierarchy

```
DeclarativeBase
  └── Base (id, created_at, updated_at, to_dict())
        ├── TenantScopedBase (+ tenant_id)
        │     ├── User, Recording, Segment, Chunk
        │     ├── TagFact, TagCurrent, TagStat
        │     ├── VectorEntity, VectorChunk
        │     └── AuditLog, LLMCallLog
        ├── Tenant (no tenant_id — self is the tenant)
        └── Prompt (global resource, not tenant-scoped)
```

## Naming Conventions

| Object    | Convention              | Example                     |
| --------- | ----------------------- | --------------------------- |
| Table     | snake_case, plural      | `tag_facts`, `audit_logs`   |
| Class     | PascalCase, singular    | `TagFact`, `AuditLog`       |
| Column    | snake_case              | `tenant_id`, `created_at`   |
| UNIQUE    | `ux_<table>_<cols>`     | `ux_users_tenant_email`     |
| INDEX     | `ix_<table>_<cols>`     | `ix_recordings_tenant_store`|
| CHECK     | `ck_<table>_<rule>`     | `ck_users_role`             |
| FK        | `fk_<table>_<col>`      | `fk_segments_recording_id`  |

## Enum Strategy

Enums use `String(N)` + `CheckConstraint` (not SQL ENUM) to avoid ALTER ENUM
migration pain. Python `enum.Enum` classes in `enums.py` provide application-layer
type safety.

## Usage

```python
from audio_graphy.models import Recording, Segment, TagFact, Base

# All models share Base.metadata
assert "recordings" in Base.metadata.tables
assert len(Base.metadata.tables) == 13

# Create tables (for testing)
Base.metadata.create_all(engine)

# ORM operations
recording = Recording(
    tenant_id="default",
    store_id="SH001",
    path="/data/audio/2024/01/recording.wav",
)
session.add(recording)
session.commit()
```

## Key Design Decisions

- **No physical FK on `tenant_id`**: `tenant_id` (String(64)) logically references
  `tenants.code` without a physical FK to avoid DDL bloat.
- **Denormalized `tenant_id`**: `segments`, `chunks`, `tag_facts`, `tag_current`
  carry `tenant_id` (via `TenantScopedBase`) for efficient middleware-level filtering.
- **Append-only `tag_facts`**: `updated_at` exists but is never modified; rows are
  INSERT-only. Application layer enforces this.
- **Reserved word renaming**: `count` → `tag_count`, `after` → `after_value`,
  `at` → `occurred_at`/`logged_at`, `latency` → `latency_ms`.
