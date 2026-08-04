# Advanced Graph (M9) — User & Operator Guide

This document describes the **M9 R2** surface: the optional endpoints,
frontend pages, and configuration flags that make up AudioGraphy's
*Advanced Graph* feature set. The M9 R1 baseline (services + ORM models)
ships always-on behind the master flag; M9 R2 adds HTTP routes, a cron
job, and UI affordances.

## 1. Feature flag (L9)

| Flag | Default | Effect |
|------|---------|--------|
| `ENABLE_ADVANCED_GRAPH` | `False` | Master switch. When `False`, every R2 router is omitted from the FastAPI app — the paths return **404**. |
| `ENABLE_BITEMPORAL_EDGES` | `True` | Sub-flag. Ignored when master is `False`. |
| `ENABLE_LEIDEN` | `True` | Sub-flag for the Leiden admin router. |
| `ENABLE_COMPRESSION` | `True` | Sub-flag for the compression admin router. |
| `ENABLE_SPEAKER_LAYER2_FUZZY` | `True` | Sub-flag for the L8 fuzzy matcher + T13 endpoints. |

The T13 speaker merge-pending endpoints are mounted on the existing
`/speakers` router, so they remain available even when the master flag
is `False`. Every other R2 surface is gated.

## 2. Endpoints

### 2.1 Bi-temporal edges (T4)

```
GET  /api/v1/recordings/{recording_id}/edges?at=ISO&include_soft_deleted=bool
GET  /api/v1/recordings/{recording_id}/edges/range?from=ISO&to=ISO
GET  /api/v1/recordings/{recording_id}/edges/{edge_id}/history
```

* RBAC: `inspector`+ for read.
* `edge_id` is the key `"{source}|{relation}|{target}"`.
* All responses are tenant-scoped (L10).

### 2.2 Leiden admin (T6)

```
POST /api/v1/admin/leiden/recompute         {force_full: bool, triggered_by: str}
GET  /api/v1/admin/leiden/jobs/{job_id}
GET  /api/v1/admin/leiden/jobs               ?status=&limit=&offset=
GET  /api/v1/admin/leiden/status
```

* RBAC: `admin` only (inspector/viewer → 403).
* `recompute` runs **synchronously**; the returned `LeidenJob` row is
  the final state.

### 2.3 Search (T8)

```
POST /api/v1/search/global                  {query, top_k=5, level=0, community_ids?}
POST /api/v1/search/local                   {query, seed_entity_ids, depth=1, top_k=5}
POST /api/v1/search/communities/{id}/drill-down  {level}
```

* RBAC: `inspector`+ for read.
* Map-reduce concurrency is capped at **5** per L4; `top_k` default is
  **5** (max 50).
* Default scorer is keyword overlap (CJK bigrams + whitespace tokens).
  Production deployments inject an LLM scorer via the
  `GlobalSearcher(scorer=...)` constructor.

### 2.4 Compression admin (T10)

```
POST /api/v1/admin/compression/dry-run      {max_candidates, god_node_degree_threshold?, stale_days?}
POST /api/v1/admin/compression/run          {max_candidates, policy_check}
GET  /api/v1/admin/compression/history      ?limit=&offset=
```

* RBAC: `admin` only.
* Weekly cron: **Sunday 03:00 Asia/Shanghai** (registered in
  `main.py`). The cron calls `run_weekly_compression_sweep` which
  iterates every tenant with a populated graph store.

### 2.5 Speaker merge-pending (T13)

```
GET  /api/v1/speakers/merge-pending         ?status=&limit=&offset=
POST /api/v1/speakers/{pending_id}/merge/{target_id}   {voiceprint_score?, notes?}
POST /api/v1/speakers/{pending_id}/reject-merge        {notes?}
```

* RBAC: viewer+ read; inspector/admin write.
* Status transitions: `pending → resolved_inferred | resolved_rejected`.
  Re-resolving an already-resolved row returns **409**.

Repeat `status` to match several at once (the "resolved" tab asks for both
resolved states), and pass `matched_speaker_node_id` to scope the queue to
one speaker — filtering client-side after a capped page silently drops older
rows once the queue outgrows `limit`.

### 2.6 Speaker quality surfaces (ADR-0001)

```
GET  /api/v1/speakers/voiceprint-policy      -> thresholds + sampling gates
GET  /api/v1/speakers?recording_id=          -> speakers appearing in a recording
GET  /api/v1/recordings/{id}/speakers        -> spk_N label -> canonical speaker
```

* RBAC: every read endpoint here is viewer+; none carries biometric data, only
  the truncated voiceprint hash (§17.1), which is already a fingerprint. Gating
  the roster higher than the reconfirm queue only meant a viewer could see a
  merge decision without seeing the speaker it was about. Writes stay
  inspector+. `recordings/{id}/speakers` additionally names `agent`, which
  `require_role` matches by name rather than by level.
* `recordings/{id}/speakers` is what lets a transcript or timeline show who
  is speaking: segments store only the diarization-local label, so without
  this mapping every line reads `spk_0` with no identity and no confidence.
  Links written before migration `0035` have no label and are omitted rather
  than guessed at.
* The policy endpoint reads live settings, so the quality drawer cannot drift
  from the thresholds the pipeline actually applies.

## 3. Frontend pages

| Path | Component | Role |
|------|-----------|------|
| `/time-travel` | `TimeTravel/index.tsx` | Inspector+ |
| `/communities` | `CommunityExplorer/index.tsx` | Inspector+ |
| `/speakers/:id` (extended) | `SpeakerProfile/Detail.tsx` + `PendingMergesCard` | Inspector+ |
| `/speakers` (quality drawer) | `components/VoiceprintQualityDrawer.tsx` | Viewer+ reads, Inspector+ reviews |

All API calls go through `frontend/src/api/advancedGraph.ts`. When the
backend returns 404 (master flag off), the UI surfaces a friendly
"feature disabled" message and degrades to empty state.

## 4. Operation

### 4.1 Turning the feature on

```bash
export ENABLE_ADVANCED_GRAPH=true
```

Restart the backend. The startup log should print:

```
INFO audio_graphy.main: M9 advanced graph ENABLED
```

### 4.2 Forcing a Leiden recompute

```bash
curl -X POST http://localhost:8000/api/v1/admin/leiden/recompute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"force_full": true, "triggered_by": "ops"}'
```

### 4.3 Running compression manually

```bash
# Dry-run first (no mutations):
curl -X POST http://localhost:8000/api/v1/admin/compression/dry-run \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"max_candidates": 50}'

# Apply:
curl -X POST http://localhost:8000/api/v1/admin/compression/run \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"max_candidates": 50, "policy_check": true}'
```

## 5. Quality contract

* All R2 endpoints return **404** when `enable_advanced_graph=False`.
* All R2 endpoints enforce `tenant_id` from the JWT (L10).
* The M1-M8 regression suite (`tests/regression/test_m1_m8_unchanged.py`)
  must pass at flag=False.
* Coverage target ≥ 88%.
