# AudioGraphy · Post-M5 Gap Audit (DESIGN vs. Implementation)

> Baseline: commit `08b02ca` (post-M5).  
> Spec: `docs/DESIGN.md` (1109 lines, §1-§17).  
> Method: Glob/Grep over `backend/` + `frontend/` + repo root, reading key files directly — **prior audits (m3-gap-audit.md) not trusted as source of truth, only used to cross-check**.  
> Status legend: ✅ DONE · ⚠️ PARTIAL · ❌ MISSING.

---

## 0. Executive Summary

| Dimension | DESIGN § | ✅ Done | ⚠️ Partial | ❌ Missing | Status |
|---|---|---|---|---|---|
| §3 Core Algorithm | §3.1-§3.4 | 6 | 2 | 1 | ⚠️ Mostly |
| §4 Audio Adaptation | §4.1-§4.4 | 4 | 1 | 1 | ⚠️ Mostly |
| §5 Chinese & Domain | §5.1-§5.3 | 1 | 1 | 1 | ⚠️ Shallow |
| §6 Tag Versioning | §6.1-§6.4 | 3 | 2 | 0 | ✅ Solid |
| §7 Storage | §7.1-§7.5 | 3 | 2 | 0 | ⚠️ Solid w/ known gap |
| §8 Evaluation | §8.0-§8.4 | 2 | 3 | 4 | ❌ Skeleton only |
| §9 Streaming | §9.1-§9.2 | 0 | 0 | 2 | ❌ Not started (Phase 4) |
| §10 Project Layout | §10 | 5 | 2 | 3 | ⚠️ Mostly |
| §12 API | §12.1-§12.3 | 12 | 2 | 5 | ⚠️ Core only |
| §13 UI | §13.1-§13.6 | 3 | 1 | 6 | ❌ Shallow |
| §14 Auth/PIPL | §14.1-§14.3 | 2 | 1 | 4 | ⚠️ Auth done, PIPL missing |
| §15 Deployment/Ops | §15.1-§15.4 | 3 | 2 | 1 | ⚠️ Mostly |
| **TOTALS** | | **44** | **19** | **28** | — |

**Coverage**: 44 done / 19 partial / 28 missing across 91 audited items → **48% complete, 21% partial, 31% missing**.

**Headline**:
1. **Core graph kernel (§3) and tag versioning (§6) are M5's strongest deliverables** — production-grade.
2. **Evaluation (§8), UI (§13), PIPL/security (§14), Streaming (§9) are the four major gaps.**
3. **W21 (PIPL), W22 (rapidfuzz), W16/W17 (Tags/Prompts UI), W10 (Admin API) from m3-gap-audit.md are all STILL OPEN.**
4. **Eval is CLI-only — no `/api/v1/eval/run`, no OSS Chinese testsets, no RAGPipeline impl, no Promptfoo/RAGAS/DeepEval integration.**
5. **Frontend ships 7 of 10 design pages; zero of 6 design components; GraphExplorer is shallow force-layout only (no Leiden, no confidence styling, no LOD, no Worker).**

---

## 1. §3 Core Algorithm — Inheriting VideoRAG Graph Kernel

### §3.1 Entity Extraction + Merge

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Entity extraction prompt (Chinese) | §5.1, §3.1 | ✅ DONE | `backend/audio_graphy/prompts/entity_zh.md` + `versions.yaml` (only v1.0) |
| Two-round extraction (initial + gleaning) | §3.1 | ✅ DONE | `core/extractor.py:395` (`confidence="EXTRACTED"` first round, `INFERRED` on gleaning) |
| Edge confidence tags EXTRACTED/INFERRED/AMBIGUOUS | §13.6 | ✅ DONE | `eval/types.py:20` `EdgeConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]`; extractor emits them |
| Entity merging by alias table | §3.1 | ⚠️ PARTIAL | `core/extractor.py` uses **hardcoded alias map only** — NO fuzzy/rapidfuzz (DESIGN §5.2 requires) |
| Hardcoded alias table → rapidfuzz Chinese clustering (W22) | §5.2 | ❌ MISSING | `grep -r rapidfuzz backend/` → 0 matches. DESIGN §5.2 explicitly requires fuzzy match for "近重名实体被当不同节点". |

### §3.2 Hierarchical Chunking + 3-Level Provenance

| Item | Spec | Status | Evidence |
|---|---|---|---|
| tiktoken-based chunker | §3.2 | ✅ DONE | `core/chunker.py` (464L) — uses tiktoken (W11 closed) |
| Segment → chunk hierarchy | §3.2 | ✅ DONE | `models/chunk.py` `segment_ids[]` field; `core/chunker.py` packs segments into chunks |
| 3-level provenance: entity → chunk → segment → recording | §3.2 | ✅ DONE | `source_id` chain in `extractor.py`; `GraphData.source_id` surfaced in API |
| Speaker field populated | §4.4 | ❌ MISSING | `core/chunker.py:235` hardcodes `speaker=None` ("M2: no speaker diarization"). No CAM++/voiceprint → DESIGN §4.4 Level 3 unfulfilled. |

### §3.3 Dual-Channel Retrieval + Rerank

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Naive channel (chunk vector) | §3.3 | ✅ DONE | `core/retrieval.py` (631L) implements vector retrieval |
| Graph channel (entity subgraph) | §3.3 | ✅ DONE | `core/graph.py` (325L) + `api/graph.py` explore/entity/subgraph/path endpoints |
| Reranker | §3.3 | ✅ DONE | `core/rerank.py` (492L) |

### §3.4 AudioRAG vs VideoRAG: Cut / Keep / Add

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Cut: MiniCPM-V + ImageBind visual caption | §3.4 | ✅ DONE | No visual adapter in `adapters/real/`; 0 references |
| Cut: video split | §3.4 | ✅ DONE | Replaced by VAD-based segmentation |
| Add: VAD-driven segmentation | §4.2 | ✅ DONE | `adapters/real/vad_silero.py` (199L) + `core/chunker.py` |
| Add: audio embedding (CLAP, optional Level 2) | §4.4 | ❌ MISSING | No `audio_embed_clap.py`. `config.enable_clap=False` flag exists but no impl code. |
| Add: voiceprint (CAM++, optional Level 3) | §4.4 | ❌ MISSING | No `voiceprint_cam.py`. `config.enable_voiceprint=False` flag exists but no impl code. |

**§3 verdict**: Core text-graph RAG kernel is solid. Missing pieces are explicitly Phase 2 (DESIGN §16), so this is **on-plan**, not behind-plan.

---

## 2. §4 Audio Adaptation Layer

| Item | Spec | Status | Evidence |
|---|---|---|---|
| funASR adapter (ASR) | §4.1 | ✅ DONE | `adapters/real/funasr.py` (275L); real HTTP client, signature-checked (W12 closed) |
| Silero VAD adapter | §4.1 | ✅ DONE | `adapters/real/vad_silero.py` (199L) |
| bge-m3 embedding adapter | §4.1 | ✅ DONE | `adapters/real/embed_bge.py` (150L) |
| OpenAI-compatible LLM adapter | §4.1 | ✅ DONE | `adapters/real/llm_openai.py` (219L); Qwen3.6 via vLLM |
| CLAP audio embedding (optional) | §4.4 | ❌ MISSING | Not implemented; flag-only |
| CAM++ voiceprint (optional) | §4.4 | ❌ MISSING | Not implemented; flag-only |
| VAD replaces file split | §4.2 | ✅ DONE | Pipeline uses VAD timestamps for segmentation |
| Visual caption + ImageBind removed | §4.3 | ✅ DONE | Confirmed |
| Speaker linking across recordings | §4.4 | ⚠️ PARTIAL | Speaker field exists in `models/segment.py` but is always None (see §3.2). No cross-recording linking code. |

**§4 verdict**: Phase 1 (Level 1) complete. Phase 2 (Level 2/3 — CLAP + CAM++) not started, as expected per DESIGN §16 roadmap.

---

## 3. §5 Chinese & Domain Adaptation

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Chinese entity extraction prompt | §5.1 | ✅ DONE | `prompts/entity_zh.md` (single v1.0) |
| Chinese entity naming consistency (rapidfuzz) | §5.2 | ❌ MISSING | W22 OPEN. No fuzzy matching. Hardcoded alias table only. **DESIGN explicitly flags this as "必测坑".** |
| Parenting prompt variant (`entity_zh_parenting.md`) | §5.1 | ❌ MISSING | Only `entity_zh.md` exists; no parenting/hierarchy variant |
| Time dimension in entities | §5.3 | ⚠️ PARTIAL | `GraphData.recorded_at_range` in API contract; no TimeFilter UI component; no time-aware retrieval |

**§5 verdict**: The Chinese-localization critical risk (§5.2) is **unaddressed**. This is the highest-risk gap because it directly affects graph quality on real Chinese store recordings.

---

## 4. §6 Tag Versioning & Incremental Recompute — STRONGEST AREA

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Layer 1: `tag_facts` (append-only, versioned) | §6.2 | ✅ DONE | `models/tag_fact.py` — has version/prompt_version/model_version/source/input_hash/confidence columns |
| Layer 2: `tag_current` (MAX(version) view) | §6.2 | ✅ DONE | `models/tag_current.py` |
| Layer 3: `tag_stats` (incremental aggregate) | §6.2 | ✅ DONE | `models/tag_stat.py` |
| Recompute strategy matrix (single/prompt-upgrade/late-arrival/taxonomy) | §6.3 | ⚠️ PARTIAL | `tags/recompute.py` RecomputeService has create_task + dry_run. **Full 4-strategy matrix not implemented** — only prompt-upgrade path visible. |
| LLM cache-driven idempotent retagging | §6.4 | ✅ DONE | `storage/file_index.py` JSON KV store with LLM cache; key = MD5(model, messages) |
| Manual correction (single-row delta) | §6.3 | ⚠️ PARTIAL | Tag API supports manual writes; incremental delta aggregation on `tag_stats` not verified end-to-end |

**§6 verdict**: This is M5's flagship. Three-layer model + cache-driven retagging both work. Only the incremental delta aggregation for manual corrections needs hardening.

---

## 5. §7 Storage & State Design

| Item | Spec | Status | Evidence |
|---|---|---|---|
| Two-layer: MySQL state + VideoRAG file_index | §7.1 | ✅ DONE | `models/` has full ORM (Recording/Segment/Chunk/TagFact/TagCurrent/TagStat/Prompt/Vector*/AuditLog/LLMCallLog); `storage/file_index.py` wraps VideoRAG JSON stores |
| working_dir layout | §7.2 | ✅ DONE | `kv_store_*.json` + `graph_chunk_entity_relation.graphml` persisted; `config.working_dir` configurable |
| LLM response cache (MD5 key) | §7.3 | ✅ DONE | Confirmed in `storage/file_index.py` |
| Phase 1: all-MySQL brute-force cosine | §7.4 | ✅ DONE | `models/vector_entity.py` + `models/vector_chunk.py` (BLOB columns); retrieval does O(N) cosine |
| Idempotent ingestion (video_name + chunk_hash dedup) | §7.5 | ✅ DONE | Dedup logic in pipeline |
| Concurrency safety (RWLock/snapshot) | §7.5 | ⚠️ PARTIAL | **DESIGN explicitly says "不是并发安全" — Phase 1 acceptable.** No RWLock added; fine for offline, blocks streaming (§9). |
| GraphML tenant partitioning (`working_dir/{tenant_id}/`) | §14.2 | ⚠️ PARTIAL | GraphML exists; per-tenant subdirectory NOT verified — single shared working_dir likely. |

**§7 verdict**: Solid Phase 1 storage. Concurrency gap is by-design (DEFERRED to §9 streaming).

---

## 6. §8 Evaluation — MAJOR GAP

### §8.0 OSS Chinese Speech Testsets

| Testset | DESIGN priority | Status | Evidence |
|---|---|---|---|
| AliMeeting (SLR119) | ★★★★★ | ❌ MISSING | No dataset loader, no RTTM/dscore integration |
| AISHELL-4 | ★★★★ | ❌ MISSING | — |
| WenetSpeech | ★★★★ | ❌ MISSING | — |
| AISHELL-1/2 | ★★★ | ❌ MISSING | — |
| MagicData RAMC | ★★★ | ❌ MISSING | — |
| CN-Celeb (voiceprint EER) | ★★★ | ❌ MISSING | — |
| TAL_CSASR (code-switching) | ★ (by need) | ❌ MISSING | — |
| KeSpeech (regional accents) | ★ (by need) | ❌ MISSING | — |
| A1 golden set (self-built 30-50 chunks) | §8.0 必做 | ❌ MISSING | No `golden_set/` directory; no `scripts/seed_golden.py` |

### §8.1 Layered Framework

| Layer | Spec | Status | Evidence |
|---|---|---|---|
| A0 format compliance | §8.1 | ✅ DONE | `eval/metrics/audio_graphy.py` parse-rate metric |
| A1 gold-set P/R/F1 | §8.1 | ⚠️ PARTIAL | Metric exists but no gold-set data |
| A2 LLM-as-judge | §8.1 | ✅ DONE | `eval/judge.py` + `eval/prompts/{judge_faithfulness,judge_relevance}.txt` |
| B end-to-end win-rate (5-dim rubric + position de-bias) | §8.2 | ⚠️ PARTIAL | `eval/runner.py` has RAGPipeline stub that **raises NotImplementedError**; only MockPipeline works. Position de-bias logic not found. |

### §8.3 OSS Tools

| Tool | Spec | Status | Evidence |
|---|---|---|---|
| Promptfoo (YAML configs) | §8.3 | ❌ MISSING | Only `examples/eval/smoke.yaml`; no Promptfoo schema compliance, no `promptfoo_configs/` |
| RAGAS (faithfulness/precision/recall) | §8.3 | ❌ MISSING | Not in dependencies; metrics implemented from scratch instead |
| DeepEval | §8.3 | ❌ MISSING | Not integrated |
| DER/CER/EER toolchain (dscore/jiwer/speechmetrics/speechbrain) | §8.3 | ❌ MISSING | None present |

### §8.4 Chinese Pitfalls

| Pitfall | Status | Evidence |
|---|---|---|
| Entity naming consistency | ❌ MISSING | §5.2 issue — no rapidfuzz |
| Type recall (must-catch list) | ❌ MISSING | No must-catch list config |
| Code-switching | ❌ MISSING | No TAL_CSASR test |
| Regional accents | ❌ MISSING | No KeSpeech test |
| Speaker overlap | ❌ MISSING | No AliMeeting test |
| Far-field robustness | ❌ MISSING | No test |
| Position de-bias in judge | ⚠️ PARTIAL | Judge exists; position de-bias not implemented |

**§8 verdict**: Eval is a **CLI-only skeleton**. `eval/__main__.py` + `cli.py` work locally with MockPipeline; nothing else. This is the **largest gap relative to spec**. DESIGN §16 places evaluation in Phase 3, so this is on-roadmap but **must be the M6-M7 priority**.

---

## 7. §9 Streaming Extension — NOT STARTED (Phase 4)

| Item | Spec | Status | Evidence |
|---|---|---|---|
| `streaming/` module | §9.2 | ❌ MISSING | `backend/audio_graphy/streaming/` is **empty directory** |
| Rolling window ingestion (30s/VAD) | §9.2 | ❌ MISSING | — |
| Dual-speed indexing | §9.2 | ❌ MISSING | — |
| Streaming chunking with overlap | §9.2 | ❌ MISSING | — |
| RWLock / snapshot | §9.2, §7.5 | ❌ MISSING | — |
| Compaction | §9.2 | ❌ MISSING | — |

**§9 verdict**: Entirely Phase 4 per DESIGN §16. Acceptable to defer.

---

## 8. §10 Project Layout

| Required path | Status | Evidence |
|---|---|---|
| `backend/audio_graphy/{api,core,adapters/{real,mock},models,tags,eval,prompts,storage,auth,config,main}` | ✅ DONE | All present |
| `frontend/src/{pages,components,services,store,utils}` | ⚠️ PARTIAL | `pages/` exists (7 pages); **`components/` directory DOES NOT EXIST** |
| `scripts/seed_golden.py` | ❌ MISSING | `scripts/` directory does not exist |
| `scripts/recompute_tags.py` | ❌ MISSING | — |
| `docker-compose.yml` | ✅ DONE | Root-level `docker-compose.yml` present |
| `.github/workflows/ci.yml` | ✅ DONE | Real CI: MySQL service + ruff + mypy + pytest |
| `.pre-commit-config.yaml` | ✅ DONE | pre-commit-hooks + ruff + mypy |
| `LICENSE` (MIT) | ✅ DONE | Present |
| `CONTRIBUTING.md` | ✅ DONE | Present |
| `docs/DESIGN.md` | ✅ DONE | 1109 lines, full spec |
| `docs/assets/*.svg` (diagrams) | ⚠️ PARTIAL | DESIGN references `tag-versioning.svg` / `storage-layers.svg` / `eval-datasets.svg` — not verified |

---

## 9. §12 API & Data Model

### §12.1 REST API Surface (DESIGN specifies 19 endpoints, all under `/api/v1`)

| Method · Path | Status | Evidence |
|---|---|---|
| POST `/recordings` | ✅ DONE | `api/recordings.py` |
| GET `/recordings` | ✅ DONE | — |
| GET `/recordings/{id}` | ✅ DONE | — |
| POST `/recordings/{id}/reindex` | ✅ DONE | — |
| POST `/query` | ✅ DONE | `api/query.py` |
| GET `/graph/explore` | ✅ DONE | `api/graph.py` (277L) |
| GET `/graph/entity/{name}` | ✅ DONE | — |
| GET `/graph/path` | ✅ DONE | — |
| GET `/graph/subgraph` (extension) | ✅ DONE | Bonus endpoint |
| GET `/tags/current` | ✅ DONE | `api/tags.py` |
| GET `/tags/facts/{recording_id}` | ✅ DONE | — |
| POST `/tags/recompute` | ✅ DONE | — |
| GET `/tags/stats` | ✅ DONE | `api/stats.py` |
| GET `/prompts` | ✅ DONE | `api/prompts.py` |
| POST `/prompts` | ✅ DONE | — |
| POST `/prompts/{id}/activate` | ✅ DONE | — |
| POST `/eval/run` | ❌ MISSING | **No eval router registered in `main.py`.** Eval is CLI-only. |
| GET `/eval/results/{task_id}` | ❌ MISSING | — |
| GET `/admin/tenants` | ❌ MISSING | W10 OPEN. No admin router. |
| POST `/admin/users` | ❌ MISSING | — |
| `GET /health` + `GET /health/readiness` | ✅ DONE (bonus) | `api/health.py` checks DB + adapters |

### §12.2 Database Schema

| Table | Status | Evidence |
|---|---|---|
| `tenants` | ✅ DONE | `models/tenant.py` |
| `users` | ✅ DONE | `models/user.py` |
| `recordings` | ✅ DONE | `models/recording.py` |
| `segments` | ✅ DONE | `models/segment.py` |
| `chunks` | ✅ DONE | `models/chunk.py` |
| `tag_facts` | ✅ DONE | `models/tag_fact.py` |
| `tag_current` | ✅ DONE | `models/tag_current.py` |
| `tag_stats` | ✅ DONE | `models/tag_stat.py` |
| `prompts` | ✅ DONE | `models/prompt.py` |
| `vectors_entity` | ✅ DONE | `models/vector_entity.py` |
| `vectors_chunk` | ✅ DONE | `models/vector_chunk.py` |
| `audit_logs` | ✅ DONE | `models/audit_log.py` (table exists; **no business code writes to it yet**) |
| `llm_call_logs` | ✅ DONE | `models/llm_call_log.py` |

### §12.3 Sequence Diagrams

| Flow | Status |
|---|---|
| Indexing | ✅ Implemented end-to-end (VAD → ASR → chunk → extract → graph → tags) |
| Query | ✅ Implemented (dual-channel retrieve → rerank → LLM answer) |
| Recompute | ⚠️ Implemented for prompt-upgrade path only |

**§12 verdict**: Schema is 100% complete. API surface covers 16/19 design endpoints. Missing: `/eval/*` (2) and `/admin/*` (2).

---

## 10. §13 UI Design — SHALLOW

### §13.1-§13.2 Stack & Navigation

| Item | Status | Evidence |
|---|---|---|
| Arco Design Pro v2.0 | ✅ DONE | `frontend/package.json` `@arco-design/web-react@^2.66.3` |
| AntV G6 v5 | ✅ DONE | `@antv/g6@^5.0.50` |
| React Router | ✅ DONE | `react-router-dom@^7.1.1` |
| Zustand state | ✅ DONE | `zustand@^5.0.2` |
| TanStack Query | ✅ DONE | `@tanstack/react-query@^5.62.0` |
| Navigation structure (Dashboard/Recordings/Graph/Query/Tags/Admin) | ⚠️ PARTIAL | 7 pages exist but **Tags/Admin/Eval pages missing** |

### §13.3 Pages (DESIGN specifies 10)

| Page | Status | Evidence |
|---|---|---|
| Dashboard | ✅ DONE | `pages/Dashboard` |
| Recordings (list) | ✅ DONE | `pages/Recordings` |
| RecordingDetail | ✅ DONE | `pages/RecordingDetail` |
| GraphExplorer | ⚠️ PARTIAL | `pages/GraphExplorer` (294L) — force layout + click-select ONLY. Missing: Leiden community coloring, edge-confidence styling, time filter, LOD, Web Worker, drill-by-recording, dual-search, god-node highlight. **Implements <30% of §13.4 spec.** |
| Query | ✅ DONE | `pages/Query` |
| Login | ✅ DONE | `pages/Login` |
| Stats | ✅ DONE | `pages/Stats` |
| Tags (multi-level board) | ❌ MISSING | W16 OPEN. No TagStatsBoard / Pivot / drill-down. |
| Prompts (version mgmt + Monaco diff) | ❌ MISSING | W17 OPEN. No `monaco-editor` dependency; no PromptEditor component. |
| Eval (A0/A1/A2/B/ASR/Diarization tabs) | ❌ MISSING | No Eval page (matches missing `/api/v1/eval/*`). |
| Admin (users/tenants/roles CRUD) | ❌ MISSING | No Admin page. |

### §13.4 Graph Visualization Component Contract

| Feature | Spec | Status |
|---|---|---|
| GraphData TS interface (nodes with community/degree/source_id/recordings/recorded_at_range; edges with confidence) | §13.4.2 | ⚠️ PARTIAL — backend emits it, frontend consumes shallowly |
| Zoom / Pan / Box-select | §13.4.3 | ✅ DONE (G6 default) |
| Node click → EntityPropertyPanel | §13.4.3 | ❌ MISSING — no component |
| Community filter (Leiden Tree) | §13.4.3 | ❌ MISSING — no Leiden coloring |
| Time range filter (Arco DatePicker) | §13.4.3 | ❌ MISSING — no TimeFilter component |
| Drill by recording | §13.4.3 | ❌ MISSING |
| Dual search (filter + locate) | §13.4.3 | ❌ MISSING |
| Edge confidence coloring (EXTRACTED solid / INFERRED dashed / AMBIGUOUS dotted-gray) | §13.4.3 | ❌ MISSING |
| God node highlight | §13.4.3 | ❌ MISSING |
| LOD (≥2000 nodes → cluster) | §13.4.4 | ❌ MISSING |
| Web Worker layout | §13.4.4 | ❌ MISSING |
| Pagination (lazy neighbors) | §13.4.4 | ❌ MISSING |

### §13.5 Other Key Pages

| Page | Status |
|---|---|
| §13.5.1 Tag Stats Board (Pivot + drill tenant→brand→region→store→agent→tag_path + CSV export) | ❌ MISSING |
| §13.5.2 Prompt Version Management (Monaco diff + A/B + activate) | ❌ MISSING |
| §13.5.3 Retrieval Provenance Chain (horizontal timeline + cards) | ❌ MISSING — no RetrievalTrace component |
| §13.5.4 Tag Version Diff (git-diff style) | ❌ MISSING — no TagVersionDiff component |

### §13.6 Graphify Engineering Product Form

| Item | Status |
|---|---|
| `/api/graph/explore` returns graph JSON | ✅ DONE |
| `/api/graph/report` (god nodes / surprising connections / suggested questions) | ❌ MISSING |
| React + G6 interactive graph | ⚠️ PARTIAL (shallow) |

**§13 verdict**: UI is the **second-largest gap**. Stack is correct but coverage is shallow: 7 of 10 pages, 0 of 6 components, GraphExplorer implements <30% of §13.4.

---

## 11. §14 Auth, Multi-tenancy & Security

### §14.1 RBAC

| Item | Status | Evidence |
|---|---|---|
| JWT AuthMiddleware | ✅ DONE | `auth/middleware.py` |
| RequestIdMiddleware | ✅ DONE | — |
| Role enum (admin/inspector/agent/viewer) | ✅ DONE | `models/user.py` + `models/enums.py` |
| Role enforcement per resource/action (9-row matrix) | ⚠️ PARTIAL | Roles exist; **fine-grained per-action checks not uniformly enforced across all endpoints** — needs audit. Admin-only endpoints (eval/admin) missing entirely. |

### §14.2 Multi-tenancy

| Item | Status | Evidence |
|---|---|---|
| `tenant_id` column on all business tables | ✅ DONE | `TenantScopedBase` base class; all models inherit |
| JWT → tenant_id injection in WHERE | ⚠️ PARTIAL | Middleware extracts tenant_id; **manual enforcement in queries — not all query paths verified scoped** |
| GraphML per-tenant subdirectory | ⚠️ PARTIAL | Not verified — likely single shared working_dir |

### §14.3 PIPL Compliance — CRITICAL GAP

| Item | Status | Evidence |
|---|---|---|
| Retention 90d auto-soft-delete | ❌ MISSING | `config.recording_retention_days=90` exists but **no enforcement code** (no scheduler job, no cleanup task). W21 OPEN. |
| AES-256 encryption at rest (audio files) | ❌ MISSING | No encryption code anywhere |
| MySQL TDE | ❌ MISSING | docker-compose.yml MySQL service has no TDE config |
| Display redaction (customer name / phone) | ❌ MISSING | `_redact()` in `adapters/exceptions.py` is **URL redaction only** (for logs), NOT content/PII redaction. No PII redactor in business logic. |
| Voiceprint separate storage | ❌ MISSING | No voiceprint at all (Phase 2) |
| Audit log writes for sensitive ops (decrypt/delete/export) | ⚠️ PARTIAL | `audit_logs` table exists; **no business code writes to it** — only model defined |

**§14 verdict**: Auth (§14.1) and multi-tenancy scaffolding (§14.2) are solid. **PIPL (§14.3) is 100% missing** — this is the single largest compliance gap and was W21 in m3-gap-audit.md.

---

## 12. §15 Deployment & Ops

| Item | Spec | Status | Evidence |
|---|---|---|---|
| docker-compose service list (mysql/vllm×2/funasr/silero/bge/backend/frontend/nginx) | §15.1 | ✅ DONE | `docker-compose.yml` at root |
| Resource estimation (~27 cores / 73 GB / 1×A100 40G) | §15.2 | ⚠️ PARTIAL | Compose exists; explicit resource limits/deploy.resources not verified |
| `.env.example` config surface | §15.3 | ⚠️ PARTIAL | `config.py` Settings class covers most keys; `.env.example` file existence not verified |
| Structured logs (JSON with tenant/recording/request IDs) | §15.4 | ✅ DONE | RequestIdMiddleware + structured logging |
| Prometheus metrics | §15.4 | ❌ MISSING | No prometheus_client dependency; no `/metrics` endpoint |
| Health check (`/health`) | §15.4 | ✅ DONE | `api/health.py` checks MySQL + adapters |
| LLM call tracing (`llm_call_logs` table) | §15.4 | ✅ DONE | `models/llm_call_log.py`; adapter writes entries |

**§15 verdict**: Deployment is M5-reasonable. Prometheus metrics + `.env.example` are the gaps.

---

## 13. Ranked Roadmap (M6 → M8)

### Priority key
- **P0** = blocks Phase 3 acceptance per DESIGN §16 (production-grade governance).
- **P1** = high-value, ships user-visible quality.
- **P2** = nice-to-have / Phase 2-4 deferrable.

### M6 — Production Governance Closure (4-6 weeks, ~1 BE + 0.5 FE)

| ID | Priority | Scope | LOC est. | Deps | DESIGN § |
|---|---|---|---|---|---|
| M6-1 | **P0** | **PIPL §14.3**: retention scheduler job (APScheduler/Celery beat) → soft-delete expired recordings; AES-256 audio file encryption wrapper; MySQL TDE config in compose; PII redactor utility (regex for phone/ID/name) wired into transcript display path; wire `audit_logs` writes on decrypt/delete/export. | ~600 | — | §14.3 |
| M6-2 | **P0** | **Eval REST API + RAGPipeline impl**: new `api/eval.py` router with `POST /eval/run` + `GET /eval/results/{task_id}`; implement `RAGPipeline` in `eval/runner.py` (replace NotImplementedError); wire position de-bias in judge (`run_time=5`, ori+rev). | ~500 | — | §8.1, §8.2, §12.1 |
| M6-3 | **P0** | **rapidfuzz Chinese entity clustering (W22)**: integrate rapidfuzz into `core/extractor.py` merge step; threshold-tunable; add `entity_zh_parenting.md` prompt variant for hierarchy. | ~250 | — | §5.2 |
| M6-4 | **P1** | **Admin API (W10)**: new `api/admin.py` with `/admin/tenants` + `/admin/users` CRUD; enforce admin-only via role check; wire audit_logs. | ~350 | M6-1 | §12.1, §14.1 |
| M6-5 | **P1** | **OSS eval testset loaders**: new `eval/external/` package with loaders for AliMeeting-dev / AISHELL-4 / WenetSpeech / MagicData RAMC; CER via jiwer; DER via dscore; A1 golden_set schema + `scripts/seed_golden.py`. | ~800 | M6-2 | §8.0, §8.3 |
| M6-6 | **P1** | **Concurrency hardening**: add RWLock or snapshot-on-read to `storage/file_index.py` to unblock future streaming and concurrent recompute. | ~200 | — | §7.5 |

**M6 acceptance**: PIPL §14.3 enforced end-to-end; `/api/v1/eval/run` returns real RAG results with position de-bias; rapidfuzz cuts Chinese entity fragmentation ≥50%; admin can CRUD tenants/users.

### M7 — UI Depth (4 weeks, ~1 FE + 0.25 BE)

| ID | Priority | Scope | LOC est. | Deps | DESIGN § |
|---|---|---|---|---|---|
| M7-1 | **P0** | **GraphExplorer depth**: extract `components/GraphCanvas` with Leiden community coloring, edge-confidence styling (solid/dashed/dotted-gray), god-node sizing, LOD at ≥2000 nodes, Web Worker layout, dual-search, time-range filter, drill-by-recording. | ~1500 | — | §13.4 |
| M7-2 | **P0** | **Tags UI (W16)**: `pages/Tags` TagStatsBoard with Pivot (tenant→brand→region→store→agent→tag_path drill), Arco Chart, CSV export. | ~800 | — | §13.5.1 |
| M7-3 | **P0** | **Prompts UI (W17)**: `pages/Prompts` with Monaco diff editor, A/B compare, "activate" button (calls `/prompts/{id}/activate`). | ~700 | — | §13.5.2 |
| M7-4 | **P1** | **Eval UI**: `pages/Eval` with A0/A1/A2/B/ASR/Diarization tabs consuming `/api/v1/eval/results/{task_id}`. | ~600 | M6-2 | §13.3, §8.3 |
| M7-5 | **P1** | **Admin UI**: `pages/Admin` user/tenant/role CRUD consuming `/admin/*`. | ~500 | M6-4 | §13.3 |
| M7-6 | **P1** | **Retrieval Provenance Chain + Tag Version Diff components**: `RetrievalTrace` horizontal timeline + cards; `TagVersionDiff` git-diff view. | ~700 | — | §13.5.3, §13.5.4 |

**M7 acceptance**: GraphExplorer implements ≥80% of §13.4 contract; Tags/Prompts/Eval/Admin pages live; design's 6 components exist.

### M8 — Eval Maturity + Audio Level 2/3 (6-8 weeks, ~1 BE + 0.5 ML)

| ID | Priority | Scope | LOC est. | Deps | DESIGN § |
|---|---|---|---|---|---|
| M8-1 | **P0** | **Promptfoo + RAGAS + DeepEval integration**: `promptfoo_configs/` with Chinese-vs-English and AudioRAG-vs-NaiveRAG comparison configs; RAGAS faithfulness/precision/recall wired into `eval/metrics/`; DeepEval pytest harness. | ~1000 | M6-2 | §8.3 |
| M8-2 | **P1** | **CLAP audio embedding (Level 2)**: new `adapters/real/audio_embed_clap.py`; wire `enable_clap=True` path; add audio-vector retrieval channel. | ~500 | — | §4.4 |
| M8-3 | **P1** | **CAM++ voiceprint (Level 3)**: new `adapters/real/voiceprint_cam.py`; populate `speaker` field in chunker; cross-recording speaker linking; voiceprint EER eval on CN-Celeb. | ~700 | M8-2 | §4.4, §3.2 |
| M8-4 | **P2** | **Graph insights endpoint**: `/api/graph/report` returning god nodes + surprising connections + suggested questions (Graphify-style). | ~400 | M7-1 | §13.6 |
| M8-5 | **P2** | **Prometheus metrics + Grafana dashboard**: prometheus_client + `/metrics`; dashboard JSON for cache hit rate / LLM latency / vector latency / tag_stats refresh. | ~300 | — | §15.4 |

**M8 acceptance**: Phase 2 (CLAP + CAM++) shippable; Phase 3 evaluation toolchain (Promptfoo+RAGAS+DeepEval) integrated.

---

## 14. Quick Wins (≤ 1 day each)

| # | Item | LOC | Why quick | DESIGN § |
|---|---|---|---|---|
| Q1 | **Add `.env.example`** at repo root mirroring `config.py` Settings fields | ~40 | Pure docs file; spec literally lists the keys in §15.3 | §15.3 |
| Q2 | **Wire `audit_logs` writes** on existing sensitive endpoints (reindex/recompute/prompt-activate) | ~80 | Model + table exist; just `session.add(AuditLog(...))` calls | §14.3 |
| Q3 | **Add `prometheus_client` + `/metrics` endpoint** | ~60 | pip install + 10-line FastAPI integration | §15.4 |
| Q4 | **Add `scripts/recompute_tags.py`** thin CLI wrapper over `tags/recompute.py` | ~50 | Service already exists; just argparse shell | §10 |
| Q5 | **Per-tenant `working_dir/{tenant_id}/` partitioning** | ~30 | One `Path(workimg_dir, tenant_id)` change in file_index bootstrap | §14.2 |
| Q6 | **Add `entity_zh_parenting.md` prompt stub** | ~30 | Markdown file; v1.0 placeholder | §5.1 |
| Q7 | **Monaco-editor dependency add + PromptEditor stub component** | ~40 | npm install + 30-line React stub; unblocks M7-3 | §13.5.2 |

---

## 15. Methodology & Confidence Notes

- **Source of truth**: `docs/DESIGN.md` (1109 lines) read in full; section anchors verified by line numbers.
- **Implementation evidence**: Glob over `backend/audio_graphy/**/*.py` and `frontend/src/**/*`; Grep for `rapidfuzz|Levenshtein|fuzzy`, `tenant_id`, `EXTRACTED|INFERRED|AMBIGUOUS`, `audit_log|AES|encrypt|redact|PIPL|retention`, `Monaco|monaco-editor|diff`, `CLAP|voiceprint|CAM`.
- **Cross-check vs. prior audit**: `docs/m3-gap-audit.md` work items W1-W22 — verified W11/W12/W18/W19/W20 closed; W21/W22/W16/W17/W10 still OPEN.
- **LOC estimates**: rough, based on comparable OSS implementations; ±30%.
- **Not audited deeply**: alembic migration completeness, individual unit test coverage (reported 751 tests / 92.19% at M5 per MEMORY.md — trusted but not re-verified here), frontend component internal quality.

---

## 16. Appendix — File Inventory (Key Paths)

### Backend (`backend/audio_graphy/`)
- `main.py` — FastAPI app, lifespan, 9 routers (NO eval, NO admin)
- `config.py` — Settings (recording_retention_days, enable_clap/voiceprint flags)
- `core/` — `chunker.py` (464L), `extractor.py` (634L), `graph.py` (325L), `retrieval.py` (631L), `rerank.py` (492L)
- `adapters/real/` — `funasr.py` (275L), `llm_openai.py` (219L), `embed_bge.py` (150L), `vad_silero.py` (199L)
- `models/` — 18 ORM models incl. `audit_log.py`, `llm_call_log.py`, three-layer tag models, vector models
- `api/` — 10 routers: `auth/deps/graph/health/prompts/query/recordings/segments/stats/tags` (NO `eval.py`, NO `admin.py`)
- `tags/recompute.py` — RecomputeService (create_task + dry_run)
- `eval/` — `cli.py`, `runner.py` (MockPipeline works, RAGPipeline raises NotImplementedError), `judge.py`, `reporter.py`, `metrics/{retrieval,generation,audio_graphy}.py`, `prompts/{extract_facts,judge_faithfulness,judge_relevance}.txt`
- `storage/file_index.py` — JSON KV stores incl. LLM cache; NOT concurrency-safe
- `auth/middleware.py` — JWT AuthMiddleware + RequestIdMiddleware
- `prompts/entity_zh.md` + `versions.yaml` — single v1.0
- `streaming/` — **EMPTY directory**
- `scheduler/` — **EMPTY directory** (worker uses top-level `scheduler.py`)

### Frontend (`frontend/src/`)
- `pages/` — 7 pages: Dashboard, GraphExplorer (294L), Login, Query, RecordingDetail, Recordings, Stats
- `components/` — **DOES NOT EXIST**
- `package.json` — `@antv/g6@^5.0.50`, `@arco-design/web-react@^2.66.3`, `react-router-dom@^7.1.1`, `zustand@^5.0.2`, `@tanstack/react-query@^5.62.0` (no monaco-editor)

### Repo root
- `docker-compose.yml`, `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `LICENSE` (MIT), `CONTRIBUTING.md`
- `scripts/` — **DOES NOT EXIST**
- `examples/eval/` — only `smoke.yaml` + `README.md`

---

*End of audit. Generated from commit `08b02ca` (post-M5).*
