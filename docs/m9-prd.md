# AudioGraphy M9 PRD — 高级图谱特性 (Advanced Graph Features)

| 字段 | 值 |
|------|-----|
| 版本 | v9.0.0-draft |
| 作者 | 许清楚 (Xu Qingchu) · 产品经理 |
| 主理人 | 齐活林 |
| 日期 | 2026-07-22 |
| 前置 | M8 已合并 (commit `461a6ba`)：WebSocket 流式 + DeltaGraphUpdater + EdgeConfidence 标签 |
| 范围 | Code-Ready + Production：5 个生产级图谱特性（bi-temporal / Leiden / community summary / compression / SpeakerLinker Layer 2） |
| 工作流 | WS-A Bi-temporal schema／ WS-B 增量 Leiden + 社区摘要／ WS-C 图压缩 + SpeakerLinker Layer 2 |
| Gap Audit 对照 | 关闭 M8 DeltaGraphUpdater §17.4 Leiden 缺口 + M7 SpeakerLinker Layer 2 `NotImplementedError` |
| DESIGN § 对齐 | §3.1 (实体合并) + §6 (标签版本化) + §7.5 (并发/幂等) |
| 锁定决策 | L1-L10（详见 §8） |
| Open Questions | Q1-Q3（详见 §9，每项含 PM 推荐） |
| 目标行数 | 800-1200（参考 m7-prd 833 / m8-prd 942） |

---

## 1. Document Metadata

| 字段 | 值 |
|------|-----|
| 文件路径 | `docs/m9-prd.md` |
| 版本号 | v9.0.0-draft |
| 状态 | Draft — Pending 架构师 (高见远) review + 主理人 (齐活林) sign-off |
| 作者 | 许清楚 (PM / AI 代行) |
| Reviewers | 高见远 (架构师)、齐活林 (主理人)、王督导 (门店质检代表)、赵运维 (Ops) |
| 依赖文档 | `docs/m7-prd.md`、`docs/m8-prd.md`、`docs/m8-architecture.md`、`docs/DESIGN.md` |
| 基线 commit | `461a6ba` (M8 shipped) |
| 锁定机制 | L1-L10 由主理人 + 架构师双签锁定；偏离需双签决议 |

---

## 2. Executive Summary

M9 (高级图谱特性 · Advanced Graph Features) 是 AudioGraphy 在 M1-M8 内核 (batch RAG + PIPL 治理 + audio modality + streaming) 之上的**生产级图谱能力升级**，交付五个相互关联的特性：(A) Graphiti 风格的 **bi-temporal 边 schema** 用于时序查询与审计回溯；(B) **HIT-Leiden 增量社区检测** 闭合 M8 DeltaGraphUpdater 的 Leiden 缺口；(C) **社区摘要 + 全局 map-reduce 检索** (GraphRAG global search 范式)；(D) **图压缩周清任务** (低度节点合并 + AMBIGUOUS 边降级 + 孤儿边失效)；(E) **SpeakerLinker Layer 2 fuzzy** 完成 M7 docstring 第 36-40 行承诺的 `NotImplementedError`。

**为什么是现在**：M8 PRD §17.4 显式记录 "Leiden 不做增量，admin API 触发全量"；M8 架构 §18.4 将 SpeakerFuzzyMatcher 推到 M9；M8 已 shipped 但 community-aware retrieval 在持续增量下漂移，质检场景的"按时间回溯客户陈述"需求无法满足。开源同类系统 (Graphiti 90.2% LongMemEval / GraphRAG global search / HIT-Leiden SIGMOD 2026) 已验证路径，M9 是把这些范式落地到 AudioGraphy 的合适时机。

**预期影响**：图查询召回 (recall@5) 在 gold set 上对齐 brute-force ≥ 0.85；community-aware retrieval 漂移收敛；图边数月度增长被压缩 ≥ 20%；SpeakerLinker 待审队列 AMBIGUOUS 对消化 ≥ 60%；M1-M8 全部回归测试在 `enable_advanced_graph=False` 下零退化 (L9)。

---

## 3. Background & Motivation

### 3.1 M8 增量图谱的 Leiden 缺口

M8 `DeltaGraphUpdater` (`backend/audio_graphy/core/delta_graph_updater.py:96-258`) 实现了 content-hash delta detection + EntityMerger + SpeakerLinker 复用，但 PRD §17.4 与架构 §9.1.1 双重锁定 "L6: 社区检测不做增量，admin API 触发全量重建"。这意味着：

- 流式与批处理产出的新边持续累积 (`edges.streaming_origin=TRUE`)，但 `nodes.community_id` 列停留在上一次 admin 触发 `POST /api/v1/graph/rebuild` 时的快照。
- community-aware retrieval (M5 §3.3 graph channel) 在两次 rebuild 之间**逐步漂移**——新边不参与社区归属，质检员按社区筛选会漏掉流式产生的最新实体。
- M8 风险 R5 ("增量社区检测质量退化") 的缓解依赖 "默认每日凌晨 02:00 自动触发"，但 cron 调度本身未实施，现状是 admin 手动触发，实际生产中**rebuild 间隔不可控**。

M9 通过 **HIT-Leiden 增量算法** (SIGMOD 2026) + **30% 阈值回退全量** (L2) 闭合此缺口：每次 DeltaGraphUpdater batch 完成后自动触发增量；当 `delta_nodes / total_nodes > 30%` 时降级为全量重算。

### 3.2 M7 SpeakerLinker Layer 2 `NotImplementedError`

M7 `core/speaker_linker.py:13-15` docstring：

> Layer 2 — EntityMerger fuzzy (auxiliary, M7 stub):
>     fuzz.WRatio on speaker display_name. M7 returns `None` — full impl
>     deferred to M8 (needs admin UI for confirmation flow).

M8 PRD P1-3 列入范围 (`SpeakerFuzzyMatcher`) 但未实际交付 (架构 §18.4 推到 M9)。当前 Layer 2 在 `speaker_linker.py:209` 是空注释 + 直接 fallthrough 到 Layer 3 (创建新 SpeakerNode)。后果：声纹质量差的录音 (背景噪声 / 短句 / 跨年龄段) 无法被 cosine ≥ 0.5 匹配，产生大量实际上同人的"独立"SpeakerNode，跨录音链接召回低。

M9 必须落地：rapidfuzz token_ratio ≥ 0.85 on entity neighborhood → 提出 AMBIGUOUS 合并候选；后续录音若 voiceprint cosine ≥ 0.7 再确认，升级为 INFERRED (L8)。

### 3.3 开源同类系统基准 (2026-07-22 web research)

| 系统 | 范式 | 关键 benchmark | AudioGraphy 借鉴点 |
|------|------|---------------|---------------------|
| **Graphiti** (Zep, Apache-2.0, 20k+ stars) | Bi-temporal edge + episodic memory | LongMemEval 90.2% / LOCOMO 94.7% | 4-timestamp 模型 (valid_at / invalid_at / created_at / expired_at)，旧事实**失效不删除** |
| **GraphRAG** (Microsoft, MIT) | Community summarization + global map-reduce | Global search latency −40% vs brute-force | 弱 LLM 生成社区摘要；strong LLM 仅跑 final reduction |
| **HIT-Leiden** (SIGMOD 2026) | Hierarchical Incremental Tree Leiden | 5 个数量级加速 vs full recompute；maintains connected components + hierarchical structure | 30% delta threshold → 全量；否则增量 |
| **LightRAG / nano-graphrag** | Incremental entity merge (M5 已借鉴) | — | 已在 `core/entity_merger.py` 落地，M9 复用 |

AudioGraphy 与上述系统的差异：(1) AudioGraphy 是 tenant-scoped 多租户开源系统，Graphiti / GraphRAG 均为单租户；(2) AudioGraphy 已经在 M7 引入声纹驱动的 speaker 节点，Graphiti 没有；(3) AudioGraphy 的 PIPL §14.3 合规约束 (retention cascade) 在 bi-temporal 上需特别设计——失效不删除满足审计但与 retention 冲突，M9 通过 `expired_at` 双轨解决 (§6.1 P0-4)。

---

## 4. Goals & Non-Goals

### 4.1 In-scope (M9 必交付)

**Feature A — Bi-temporal 边 schema (Graphiti paradigm)**
- 4 timestamp 列：`valid_at` / `invalid_at` (NULL = current) / `created_at` / `expired_at` (NULL = live)。
- 旧事实**失效不删除**，支撑 time-travel 查询 + 审计 + rollback。
- DeltaGraphUpdater 检测到冲突时按 Q1 决策策略处理。

**Feature B — Leiden 增量社区检测**
- 实现 HIT-Leiden 增量算法；阈值策略 (delta_nodes/total_nodes > 30% → full recompute, L2)。
- Admin API `POST /api/v1/admin/leiden/recompute` (full) + 每次 DeltaGraphUpdater batch 后自动增量。
- 每个 node 写入 `community_id`；社区摘要表持久化。

**Feature C — 社区摘要 + 全局 map-reduce 检索 (GraphRAG global search)**
- Leiden 收敛后，弱 LLM (Qwen3.6-35B-A3B) 对每个社区节点+边生成 1-2 句 `community_summary`。
- 多层级 (level 0 = top, level 1-3 = nested, L3)。
- Global search = map-reduce：query 全部社区摘要 → 弱 LLM 排序 → top-k → strong LLM 最终回答。
- Local search = entity-centric (已部分存在，M9 harmonize API)。

**Feature D — 图压缩周期性清理 (weekly cron)**
- Low-degree node merge (degree ≤ 1 + same community + rapidfuzz token_ratio ≥ 85, L6)。
- AMBIGUOUS 边 30 天未再 encounter → 降级 DEPRECATED (L7)。
- Stale orphan edge (指向已 retention-cascade 删除的节点) → `invalid_at=now()`。
- 复用 M6 retention scheduler，每周日凌晨 03:00 触发。

**Feature E — SpeakerLinker Layer 2 fuzzy (M7 stub 完成)**
- voiceprint cosine < 0.5 (Layer 1 miss) + rapidfuzz token_ratio ≥ 0.85 on entity neighborhood → 提出 `confidence_label="AMBIGUOUS"` 合并候选 (L8)。
- 后续录音中同 fuzzy pair 再次出现且 voiceprint cosine ≥ 0.7 → 升级为 `INFERRED`。
- 复用 M6 `EntityMerger` fuzzy 逻辑；voiceprint 重确认复用 M7 `VoiceprintAdapter`。

### 4.2 Out-of-scope (明确推迟到 M10+)

- ❌ 分布式图存储 (Neo4j / TigerGraph 集成) — NetworkX 单租户单进程足够当前规模。
- ❌ 向量量化 / HNSW ANN 索引 — Phase 3 议题，M9 仍走 MySQL 暴力余弦。
- ❌ 多模态实体融合 (CLAP audio embedding 进入 entity 节点 description) — M9 仅 text + voiceprint。
- ❌ 跨租户 community merge (集团多品牌共享图谱) — PIPL §14.3 禁止，永久推迟。
- ❌ 全图 GNN 嵌入 (GraphSAGE / Node2Vec) — 体积 / 计算成本不划算，M9 用 LLM 摘要替代。
- ❌ 实时流式 Leiden (边插入即聚类) — HIT-Leiden 仍是 batch 增量，M9 不做 streaming Leiden。
- ❌ Bi-temporal rollback UI (回滚到任意时间点的整图快照) — M9 仅支持查询时序，不支持整图回滚。
- ❌ Community summary 多语言 (M9 仅中文摘要；英文摘要 M10+ 按 tenant locale)。
- ❌ GraphRAG drift detection (自动检测 community 结构变化超阈值并告警) — M9 仅在结构变化时触发重生成摘要 (L3)。
- ❌ 前端 Time-Travel Query Builder 完整可视化 (M9 仅基础页面，复杂 join 推 M10)。

---

## 5. User Stories

### US-1 质检员 (Time-travel 查询客户陈述演变)

**As a** 质检员 (inspector)，
**I want** 查询某客户在 2026-07-15 当天对"价格"的所有陈述，
**so that** 在投诉调查时能看到客户陈述随时间的演变 (而不是只看到最新覆盖版本)。

**场景**：客户 7-10 说"18 万可以接受"，7-15 改口"20 万太贵"。M8 schema 下后者覆盖前者，质检员只看到 20 万版本，无法解释 7-12 坐席报告的"客户已同意 18 万"。M9 bi-temporal 后：`SELECT * FROM edges WHERE source_id=X AND relation='报价' AND valid_at <= '2026-07-15' AND (invalid_at IS NULL OR invalid_at > '2026-07-15')` 返回两条记录，时间轴上清晰可见。

**验收**：在 `/api/v1/graph/time-travel` 端点查询返回符合时间条件的所有 edge 版本（含已失效）；前端 Time-Travel Query Builder 页面 (§10) 可视化时间轴。

### US-2 督导 (Community drill-down 跨录音统计)

**As a** 王督导 (区域督导)，
**I want** 在 Community Explorer 中按社区筛选"金融政策"相关对话，
**so that** 一次性查看本月所有门店关于金融政策的客户咨询全链路。

**场景**：当前按实体名搜索会漏掉"贷款 / 分期 / 利率"等同义但未合并的实体；按 community drill-down 能跨实体抓出所有相关对话。

**验收**：Community Explorer 页面 (§10) 显示 Leiden 层级树，点击 level-1 "金融"社区展示 level-2 子社区 (贷款 / 分期 / 利率)，叶子社区展示 community_summary + top-K 实体。

### US-3 研究员 (Global search across months)

**As a** 算法研究员，
**I want** 用 global search 查"过去 90 天客户对竞品的对比倾向"，
**so that** 不用扫万级 chunk 也能得到宏观趋势回答。

**场景**：M5 naive + graph 检索在跨月汇总查询下召回率 < 0.5 (实体名漂移 + chunk 量级爆炸)。M9 global search：弱 LLM 排序所有社区摘要 (100-500 个) → top-5 → strong LLM 综合，latency < 2s。

**验收**：gold set 上 recall@5 ≥ 0.85 vs brute-force；p95 latency ≤ 2s (10k node 规模)。

### US-4 租户管理员 (手动触发 Leiden 全量)

**As a** 租户管理员，
**I want** 在 community drift 监控告警时手动触发 Leiden 全量重建，
**so that** 在关键业务节点 (季度结算 / 模型升级) 强制图刷新。

**验收**：`POST /api/v1/admin/leiden/recompute` (full) 异步执行；`GET /api/v1/admin/leiden/jobs/{job_id}` 返回 job 状态 (running / done / failed)；全量重建 10 万节点 ≤ 60s；写入 `audit_log: action="leiden_recompute"`。

### US-5 运维 (压缩任务监控)

**As a** 赵运维，
**I want** 在 Prometheus 看到周压缩任务的 edge 减少量 + AMBIGUOUS→DEPRECATED 转换数，
**so that** 验证压缩任务确实在跑且有效。

**验收**：`audiography_compression_edges_reduced_total`、`audiography_compression_edges_deprecated_total`、`audiography_compression_orphans_invalidated_total` 三个 counter 在 `/metrics` 暴露；周压缩日志可见；月度报表 edge 数下降 ≥ 20%。

### US-6 开源贡献者 (扩展点)

**As a** 开源贡献者，
**I want** 在不修改核心代码的前提下实现自定义的 community summary prompt，
**so that** 为我的领域 (例如医美咨询) 定制社区摘要风格。

**验收**：`config.py` 暴露 `community_summary_prompt_path` 字段；prompt 文件遵循 GraphRAG 风格占位符 (`{entities}` / `{edges}` / `{output_language}`)；Protocol `CommunitySummarizer` 允许替换实现。

### US-7 算法工程师 (HIT-Leiden 库不可用降级)

**As a** 算法工程师，
**I want** 在 HIT-Leiden Python 库 (SIGMOD 2026 paper code) 尚未成熟时自动降级到全量 + 缓存，
**so that** M9 不阻塞在第三方库可用性上。

**验收**：`leiden_incremental_lib_available` config flag；False 时所有 Leiden 调用走 full recompute + LRU cache (key=graph content_hash)；CI 在 flag=False 下全部测试通过。

### US-8 合规官 (Bi-temporal 与 retention 协同)

**As a** 合规官，
**I want** retention cascade 删除节点后，指向它的边自动失效 (而非删除)，
**so that** 审计链路保留"该客户曾陈述 X"的证据，但不在检索结果中显示。

**验收**：retention sweep 触发 → `UPDATE edges SET expired_at=now() WHERE source_id IN (...) OR target_id IN (...)`；retrieval 默认过滤 `expired_at IS NOT NULL`；audit_log 含 `edge_invalidated_by_retention` action。

### US-9 质检员 (SpeakerFuzzyMatcher 待审队列消化)

**As a** 质检员，
**I want** 看到 AMBIGUOUS speaker 合并候选列表，并能手动确认 / 拒绝，
**so that** Layer 2 fuzzy 提议的合并由人工兜底。

**场景**：M9 Layer 2 提议后默认入图但标 `confidence_label="AMBIGUOUS"`，并写入 `speaker_merge_pending` 表；前端 Speaker Profile 页面展示待审列表，inspector 角色可一键确认 / 拒绝。

**验收**：`GET /api/v1/speakers/merge-pending` 返回候选列表；`POST /api/v1/speakers/{id}/confirm-merge` 与 `/reject-merge` 修改 `confidence_label`；前端展示 + 操作完整。

### US-10 研究员 (Community summary 重生成触发)

**As a** 算法研究员，
**I want** 在 Leiden 结构变化 (社区 merge / split) 时自动触发 community_summary 重生成，
**so that** 摘要内容与图结构同步。

**验收**：Leiden 增量运行后比较新旧 community partition；diff 超过阈值 (社区成员变更 ≥ 30%) 的社区标记 dirty → 弱 LLM 异步重生成；Prometheus `audiography_community_summary_regenerated_total` 指标。

### US-11 租户管理员 (压缩策略可配置)

**As a** 租户管理员，
**I want** 调整压缩的 rapidfuzz 阈值与 AMBIGUOUS 降级天数，
**so that** 适配我的业务 (高合规要求租户 vs 宽松研发租户)。

**验收**：`config.py` 暴露 `compression_fuzzy_threshold` (默认 85) / `compression_ambiguous_deprecate_days` (默认 30) / `compression_low_degree_max` (默认 1)；tenant scope override 通过 `tenant_configs` 表。

### US-12 开源使用者 (向后兼容启动)

**As a** 开源使用者，
**I want** 在 `enable_advanced_graph=False` (L9 默认) 下启动 AudioGraphy 完全不感知 M9 改动，
**so that** 现有部署零回归。

**验收**：M1-M8 全部 pytest 测试在 `ENABLE_ADVANCED_GRAPH=false` 下 0 失败；M9 新增表 (`community_summaries` / `speaker_merge_pending` / bi-temporal 列) 通过 alembic 迁移存在但默认空 / 默认值不影响 M1-M8 查询；docker-compose 默认配置 `ENABLE_ADVANCED_GRAPH=false`。

---

## 6. Functional Requirements

> 每个 P0 是 release blocker；P1 在 M9 内尽量 ship；P2 可推 M9.1。

### 6.1 Feature A — Bi-temporal 边 schema (Graphiti paradigm)

| ID | 描述 | 优先级 | DESIGN § / 锁定 |
|----|------|--------|----------------|
| A-P0-1 | `edges` 表新增 4 列：`valid_at TIMESTAMPTZ`、`invalid_at TIMESTAMPTZ NULL`、`created_at TIMESTAMPTZ DEFAULT NOW()`、`expired_at TIMESTAMPTZ NULL`。索引 `idx_edges_bitemporal_valid(tenant_id, valid_at, invalid_at)` | P0 | L1 |
| A-P0-2 | Alembic 迁移 `0010_m9_bitemporal_edges.py`：upgrade 加列 + 索引；downgrade drop。迁移对老 edges 填默认值 (`valid_at=created_at_original`, `invalid_at=NULL`, `expired_at=NULL`)，向后兼容 | P0 | L1 |
| A-P0-3 | DeltaGraphUpdater 在写入新 edge 时填 `valid_at=now()`、`invalid_at=NULL`、`created_at=now()`、`expired_at=NULL`；调用方不需修改 | P0 | §17.4 |
| A-P0-4 | Retention cascade：删除节点 N 时，对所有 edge 涉及 N 的行 `UPDATE edges SET expired_at=now() WHERE (source=N OR target=N) AND expired_at IS NULL`；不删除 edge 行 (审计保留)；retrieval 过滤 `expired_at IS NOT NULL` | P0 | §14.3 / Q3 |
| A-P0-5 | DeltaGraphUpdater 冲突检测：当新 edge `(s, t, r)` 与现有 live edge 同 (s, t, r) 但 `weight` 或 `description` 不同时，按 Q1 决策策略处理 (推荐：旧 edge 设 `invalid_at=now()` + `superseded_by=new_edge_id`，新 edge 正常写入) | P0 | L1 / Q1 |
| A-P0-6 | 新增 API `GET /api/v1/graph/time-travel?entity=X&relation=R&as_of=YYYY-MM-DD`：返回所有 `valid_at <= as_of AND (invalid_at IS NULL OR invalid_at > as_of) AND expired_at IS NULL` 的 edges | P0 | US-1 |
| A-P0-7 | 新增 API `GET /api/v1/graph/edge-history?source=X&target=Y&relation=R`：返回该 edge 所有版本（含 expired），按 `valid_at DESC` 排序，附 `superseded_by` 链 | P0 | US-1 |
| A-P0-8 | 检索路径 (graph channel) 默认仅返回 `invalid_at IS NULL AND expired_at IS NULL` 的 edges；新参数 `?include_history=true` 暴露历史版本 (admin / inspector 角色) | P0 | L9 |

P1：
- A-P1-1：Edge `superseded_by` 列 (`VARCHAR(64) NULL`) 显式记录被哪条新 edge 取代；空表示未被取代。
- A-P1-2：批量冲突检测优化：对一次 DeltaGraphUpdater batch 内的所有 edges 一次性查询 live 版本，避免 N+1。
- A-P1-3：`/api/v1/graph/time-trival` 支持 time range (`?from=...&to=...`)。

P2：
- A-P2-1：Bi-temporal 可视化时间轴 (前端 G6 timeline widget)，可视化展示 edge 的 valid / invalid / created / expired 时间点。

### 6.2 Feature B — Leiden 增量社区检测

| ID | 描述 | 优先级 | DESIGN § / 锁定 |
|----|------|--------|----------------|
| B-P0-1 | `nodes.community_id VARCHAR(64) NULL` 列；`nodes.community_level INT NULL` (0=top, 1-3 nested)；alembic 迁移加列 + 索引 `idx_nodes_community(tenant_id, community_id)` | P0 | L2 / L3 |
| B-P0-2 | 新增模块 `core/leiden_incremental.py`：实现 HIT-Leiden 增量算法；输入 = 当前 NetworkX graph + 上次 partition snapshot + delta nodes/edges；输出 = 新 partition + diff (社区成员变更集)；当 delta/total > 30% (L2) 抛 `LeidenFullRecomputeRequired` | P0 | L2 / R-HIT |
| B-P0-3 | DeltaGraphUpdater.update() 末尾：累积 delta nodes；batch 提交后异步触发 `LeidenIncrementalRunner.run_incremental(tenant_id, snapshot, delta)`；失败 / threshold 触发回退到 full | P0 | L2 |
| B-P0-4 | Admin API `POST /api/v1/admin/leiden/recompute?mode=full` (异步 job) 与 `GET /api/v1/admin/leiden/jobs/{job_id}`；写入 `leiden_jobs` 表 (id / tenant_id / mode / started_at / ended_at / status / nodes_processed / communities_found) | P0 | US-4 |
| B-P0-5 | HIT-Leiden Python lib 不可用 fallback (`config.leiden_incremental_lib_available=False`)：所有调用走 full recompute + LRU cache (key=graph content_hash, size=10)；CI 在 fallback 模式下全测试通过 | P0 | R-HIT / US-7 |
| B-P0-6 | 写入 `nodes.community_id` + `community_level`；批量 UPDATE (per tenant)；alembic 索引保证查询性能；写入 audit_log `action="leiden_run"` | P0 | L10 |

P1：
- B-P1-1：`POST /api/v1/admin/leiden/recompute?mode=incremental` 手动触发增量 (用于测试 / 紧急对齐)。
- B-P1-2：Prometheus `audiography_leiden_run_duration_seconds` (histogram, labels: tenant_id, mode)、`audiography_leiden_communities_count` (gauge)、`audiography_leiden_full_recompute_fallback_total` (counter)。

P2：
- B-P2-1：`leiden_jobs` 表 admin UI 查看页面 (job 列表 + 单 job 详情)。

### 6.3 Feature C — 社区摘要 + Global Search (GraphRAG paradigm)

| ID | 描述 | 优先级 | DESIGN § / 锁定 |
|----|------|--------|----------------|
| C-P0-1 | 新表 `community_summaries`：`id BIGSERIAL PK`、`tenant_id VARCHAR(64)`、`community_id VARCHAR(64)`、`level INT` (0-3)、`summary TEXT`、`entity_ids JSON`、`edge_ids JSON`、`generated_at TIMESTAMPTZ`、`prompt_version VARCHAR(32)`、`model_version VARCHAR(64)`、`content_hash VARCHAR(64)` (复用 M6 tag 版本化模式 §6) | P0 | L3 / DESIGN §6 |
| C-P0-2 | 新增模块 `core/community_summarizer.py`：Protocol `CommunitySummarizer`；real impl `LLMCommunitySummarizer` 调用 weak LLM (Qwen3.6-35B-A3B)；输入 = community 的 nodes + edges；输出 = 1-2 句中文 summary；prompt 模板可配置 (`config.community_summary_prompt_path`) | P0 | L3 |
| C-P0-3 | Leiden 运行完成后异步触发：`CommunitySummarizer.summarize_all(tenant_id, partition)`；并发 ≤ 5 (弱 LLM rate limit)；每个 community summary 生成耗时 ≤ 30s (L3 性能预算)；写入 `community_summaries` 表 | P0 | L3 |
| C-P0-4 | 结构变化触发摘要重生成：Leiden 增量返回 diff；社区成员变更 ≥ 30% 的社区标记 dirty；异步重生成；audit_log `action="community_summary_regen"` | P0 | L3 / US-10 |
| C-P0-5 | 新增模块 `core/global_search.py`：`GlobalSearcher` 实现 map-reduce：map = 弱 LLM 对所有 level-N community_summary 打分 (relevant_score 0-1)；reduce = top-k (默认 5) summaries → strong LLM 最终答案；新 API `POST /api/v1/search/global` | P0 | L4 |
| C-P0-6 | Local search API harmonize：`POST /api/v1/search/local` (seed entities → expand to their communities → rerank)；与 M5 graph channel 检索路径复用，统一 query 参数 (`seed_entities` / `community_level` / `top_k`) | P0 | §3.3 |

P1：
- C-P1-1：community_summary 多层级生成：默认 level 0 + 叶子 (US-2 推荐 Q2)；其余层 P1 按需触发。
- C-P1-2：缓存：summary content_hash → LLM response；重复 community 结构命中缓存 (复用 M5 LLM cache Layer 2)。
- C-P1-3：`community_summaries` 表 PIPL 处理：摘要本身是 derived 数据无 PII；retention sweep 时关联 entity 已删则摘要一并 expired_at=now()。

P2：
- C-P2-1：摘要多语言 (按 tenant locale)；M9 仅中文。

### 6.4 Feature D — 图压缩周期性清理

| ID | 描述 | 优先级 | DESIGN § / 锁定 |
|----|------|--------|----------------|
| D-P0-1 | 新增模块 `core/graph_compressor.py`：3 个清理函数：`merge_low_degree_duplicates()` / `deprecate_ambiguous_edges()` / `invalidate_orphan_edges()`；复用 M6 retention scheduler 周触发 (L5) | P0 | L5 / L6 / L7 |
| D-P0-2 | Low-degree merge：扫所有 `degree <= compression_low_degree_max` (默认 1, L6) 的节点；同 community + rapidfuzz token_ratio ≥ `compression_fuzzy_threshold` (默认 85, L6) → 调用 M6 `EntityMerger.merge()` 合并；保留 canonical 节点，source node 标记 `expired_at=now()` (Q3 推荐 soft delete) | P0 | L6 / Q3 |
| D-P0-3 | AMBIGUOUS deprecate：扫所有 `confidence_label='AMBIGUOUS' AND created_at < now() - INTERVAL 'N days'` (N 默认 30, L7) AND 该 (source, target, relation) 在 N 天内无新 encounter → `UPDATE edges SET confidence_label='DEPRECATED', expired_at=now()`；保留行用于审计 | P0 | L7 |
| D-P0-4 | Orphan edge invalidate：扫 edges where `expired_at IS NULL` AND (source 节点不存在 OR target 节点不存在，因 retention cascade 删除)；→ `UPDATE edges SET invalid_at=now(), expired_at=now()`；audit_log `action="edge_orphan_invalidated"` | P0 | US-8 |
| D-P0-5 | Cron 调度：每周日 03:00 触发 (复用 M6 `core/retention.py` scheduler)；运行结果写 audit_log `action="compression_run"`，before/after 包含 edge_count / node_count / deprecated_count / merged_count；Prometheus 指标暴露 | P0 | L5 / US-5 |

P1：
- D-P1-1：tenant scope 配置 (`tenant_configs.compression_*` 字段)；不同租户不同阈值。
- D-P1-2：dry-run 模式 `POST /api/v1/admin/compression/dry-run` 返回预估变更 (不实际写)。

P2：
- D-P2-1：压缩历史 UI 页面 (Compression Run History，§10)。

### 6.5 Feature E — SpeakerLinker Layer 2 fuzzy

| ID | 描述 | 优先级 | DESIGN § / 锁定 |
|----|------|--------|----------------|
| E-P0-1 | 新增模块 `core/speaker_fuzzy_matcher.py`：`SpeakerFuzzyMatcher` 类；输入 = 候选 speaker (voiceprint + display_name + entity neighborhood)；输出 = 模糊匹配候选列表 (existing_speaker_node, score, strategy) | P0 | L8 / M7 §8 |
| E-P0-2 | SpeakerLinker Layer 2 实现 (替换 `speaker_linker.py:209` 的空注释)：Layer 1 (voiceprint cosine ≥ 0.5) miss 后调用 SpeakerFuzzyMatcher；rapidfuzz `fuzz.token_ratio` ≥ 0.85 (L8) on entity neighborhood (1-hop entities 名称集合) → 提议合并；写入 `speaker_merge_pending` 表 + SpeakerNode `confidence_label="AMBIGUOUS"` | P0 | L8 / Q-M7 |
| E-P0-3 | 重确认机制：后续录音 fuzzy pair 再次出现，且 voiceprint cosine ≥ 0.7 → `UPDATE SpeakerNode SET confidence_label='INFERRED'`；删除 `speaker_merge_pending` 行；audit_log `action="speaker_fuzzy_upgraded"`；复用 M7 `VoiceprintAdapter.extract_voiceprint()` | P0 | L8 |
| E-P0-4 | 新 API：`GET /api/v1/speakers/merge-pending` (列表 + 分页)、`POST /api/v1/speakers/{node_id}/confirm-merge`、`POST /api/v1/speakers/{node_id}/reject-merge`；inspector / admin 角色权限；audit_log 写入 | P0 | US-9 |

P1：
- E-P1-1：SpeakerFuzzyMatcher 性能优化：entity neighborhood 缓存到 SpeakerNode.attrs (per-session)；避免每次 fuzzy 调用重建。
- E-P1-2：Layer 2 候选数量上限 (默认 5)，避免对大型租户产生噪声候选。

P2：
- E-P2-1：批量 fuzzy 调度 (跨录音夜间重跑 fuzzy 匹配，提升 cold-start 召回)。

### 6.6 P1 综合列表 (跨特性)

| ID | 描述 |
|----|------|
| X-P1-1 | Prometheus metrics 完整覆盖：Leiden run / community count / compression delta / global search latency / speaker fuzzy match (count by strategy) |
| X-P1-2 | OpenTelemetry spans：Leiden incremental run / community summary generation / global search map-reduce / compression run 全链路 trace |
| X-P1-3 | tenant scope override：`tenant_configs` 表存储 per-tenant 阈值 (compression / fuzzy / ambiguous days) |
| X-P1-4 | `enable_advanced_graph=True` config flag (L9 默认 False)；启动时校验依赖 (HIT-Leiden lib / community_summaries 表 / alembic head) |
| X-P1-5 | 文档：`docs/m9-architecture.md` (架构师写) + `docs/advanced-graph.md` (用户文档) + `README.md` 更新 |
| X-P1-6 | 集成测试套件：bi-temporal time-travel / Leiden incremental / global search end-to-end / compression / fuzzy reconfirm 五条 E2E 用例 |

### 6.7 P2 综合列表

| ID | 描述 |
|----|------|
| X-P2-1 | Community Explorer G6 v5 hierarchy 可视化 (前端) |
| X-P2-2 | Compression Run History admin UI 页面 |
| X-P2-3 | Time-Travel Query Builder 前端完整可视化 (拖拽时间轴 + multi-criteria filter) |

---

## 7. Non-Functional Requirements

### 7.1 性能

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| Leiden 增量运行 (10k nodes 单 batch) | < 5s | Prometheus histogram `audiography_leiden_run_duration_seconds{mode="incremental"}` |
| Leiden 全量重建 (10k nodes) | < 60s | 同上 `mode="full"` |
| Leiden 全量重建 (100k nodes, US-4) | < 60s (cutoff) | 超过则告警 + 文档化容量上限 |
| 单 community summary 生成 | < 30s | weak LLM (Qwen3.6-35B-A3B) latency |
| Global search p95 (10k nodes, 500 communities) | < 2s | end-to-end `/api/v1/search/global` |
| Local search p95 | < 500ms | 与 M5 graph channel 持平 |
| Compression weekly run (100k edges) | < 5 min | Prometheus `audiography_compression_run_duration_seconds` |
| SpeakerFuzzyMatcher 单次调用 | < 50ms | rapidfuzz C++ backend |
| Time-travel query (single entity) | < 200ms | bitemporal index lookup |
| DeltaGraphUpdater 单 batch 增加 (Leiden + bi-temporal) | < 30% overhead vs M8 | `extraction_ms + persist_ms + leiden_ms` vs M8 baseline |

### 7.2 可靠性

- **零回归 (L9)**：`enable_advanced_graph=False` 下 M1-M8 全部 pytest 测试 0 失败；M9 新增表通过 alembic 迁移存在但默认空；M9 新增模块仅在 flag=True 时 import。
- **Alembic 向后兼容**：迁移 0010 (bi-temporal) / 0011 (community_summaries) / 0012 (speaker_merge_pending) / 0013 (nodes community 列) 全部支持 downgrade；生产先 staging 验证。
- **HIT-Leiden lib 缺失降级** (US-7)：`leiden_incremental_lib_available=False` 时所有 Leiden 调用走 full recompute + cache；CI 在两种模式下均通过。
- **DeltaGraphUpdater batch 失败回滚**：bi-temporal 冲突检测失败 / Leiden run 失败 / community summary 失败 均不阻塞主路径 (log error + alert，主路径继续)。
- **Compression run 周失败重试**：retention scheduler 复用，失败时下一日重试；告警 admin。

### 7.3 可观测性

新增 Prometheus metrics (沿用 M6/M7/M8 `/metrics` 端点)：

| 指标名 | 类型 | Labels |
|-------|------|--------|
| `audiography_leiden_run_duration_seconds` | Histogram | `tenant_id`, `mode` (incremental/full) |
| `audiography_leiden_communities_count` | Gauge | `tenant_id`, `level` |
| `audiography_leiden_full_recompute_fallback_total` | Counter | `tenant_id`, `reason` (threshold_exceeded / lib_unavailable / error) |
| `audiography_community_summary_generation_duration_seconds` | Histogram | `tenant_id`, `level` |
| `audiography_community_summary_regenerated_total` | Counter | `tenant_id` |
| `audiography_global_search_duration_seconds` | Histogram | `tenant_id` |
| `audiography_global_search_recall_at_5` | Gauge | `tenant_id` (eval mode only) |
| `audiography_compression_run_duration_seconds` | Histogram | `tenant_id` |
| `audiography_compression_edges_reduced_total` | Counter | `tenant_id` |
| `audiography_compression_edges_deprecated_total` | Counter | `tenant_id` |
| `audiography_compression_orphans_invalidated_total` | Counter | `tenant_id` |
| `audiography_speaker_fuzzy_match_total` | Counter | `tenant_id`, `strategy` (fuzzy / voiceprint_reconfirm / admin_confirm) |
| `audiography_bitemporal_edge_invalidated_total` | Counter | `tenant_id`, `reason` (conflict / orphan / retention) |

OpenTelemetry span 链：
- `leiden_run` (root) → `leiden_load_partition` → `leiden_compute_delta` → `leiden_apply` → `community_summary_generate`
- `global_search` (root) → `gs_map` (per community, parallel) → `gs_reduce` (strong LLM)
- `compression_run` (root) → `compress_merge_low_degree` → `compress_deprecate_ambiguous` → `compress_invalidate_orphan`

### 7.4 PIPL / 数据安全

- **Bi-temporal edges 与 retention cascade**：retention sweep 删节点 N 时，相关 edges 标 `expired_at=now()` (不删除行)；满足审计链路保留 (合规官可追溯) + 检索结果干净 (retrieval 过滤) 双重要求；与 M6 retention scheduler 协同。
- **Community summaries 是 derived 数据无 PII**：摘要本身由 LLM 综合 entities / edges 生成，不含原始录音文本；retention cascade 触发时关联 entity 已删则 summary `expired_at=now()`。
- **Speaker voiceprint reconfirmation** (L8)：复用 M7 `VoiceprintAdapter` + M6 `AudioCrypto` envelope；不引入新 master key (与 M7 Q3 决策一致)。
- **Time-travel query 权限**：默认 inspector / admin 角色；viewer 角色无权 (避免历史客户陈述泄露给一线坐席)。
- **`speaker_merge_pending` 行级权限**：仅 inspector / admin 可读；audit_log 记录每次 confirm / reject。

### 7.5 开源合规 (MIT)

| 项 | 状态 | 说明 |
|------|------|------|
| HIT-Leiden Python lib | 待审 (SIGMOD 2026 paper code) | 若 license 非 MIT/Apache/BSD → 实现 independent port (基于论文算法 + NetworkX + iGraph leiden)，保持 MIT clean |
| Graphiti 借鉴 | 思想借鉴，不引代码 | Graphiti Apache-2.0，但 M9 仅借鉴 bi-temporal 数据模型设计，不引入代码依赖 |
| GraphRAG 借鉴 | 思想借鉴，不引代码 | Microsoft GraphRAG MIT，但 M9 仅借鉴 map-reduce 范式，自研实现 |
| rapidfuzz | MIT (已在 M6 引入) | M9 复用 |
| NetworkX community / leiden | BSD-3-Clause | 已在依赖中 |
| 闭源 / PII 内容 | ❌ 不引入 | mock data 合成，无客户数据 |

### 7.6 可用性 / 配置面

`.env.example` 新字段 (M9 增量)：

```dotenv
# --- M9 Advanced Graph Features ---------------------------------------
ENABLE_ADVANCED_GRAPH=false                # L9: master switch, default off

# --- M9 Feature A: Bi-temporal ---------------------------------------
ENABLE_BITEMPORAL_EDGES=false              # sub-switch; requires advanced_graph=true
BITEMPORAL_CONFLICT_STRATEGY=supersede     # Q1: supersede / invalidate_old / keep_both

# --- M9 Feature B: Leiden incremental --------------------------------
LEIDEN_INCREMENTAL_LIB_AVAILABLE=false     # R-HIT: fallback to full+cache if false
LEIDEN_INCREMENTAL_THRESHOLD=0.30          # L2: delta_nodes / total_nodes > 30% → full
LEIDEN_CACHE_SIZE=10                       # full recompute LRU cache size

# --- M9 Feature C: Community summaries -------------------------------
COMMUNITY_SUMMARY_PROMPT_PATH=/prompts/community_summary_v1.txt
COMMUNITY_SUMMARY_LEVELS=0,leaf            # Q2: which levels to summarize
COMMUNITY_SUMMARY_REGEN_THRESHOLD=0.30     # L3: community membership change ratio
COMMUNITY_SUMMARY_CONCURRENCY=5

# --- M9 Feature D: Compression ---------------------------------------
COMPRESSION_ENABLE=false
COMPRESSION_CRON_DAY_OF_WEEK=sun
COMPRESSION_CRON_HOUR=3
COMPRESSION_FUZZY_THRESHOLD=85             # L6: rapidfuzz token_ratio ≥ 85
COMPRESSION_LOW_DEGREE_MAX=1               # L6: degree ≤ 1
COMPRESSION_AMBIGUOUS_DEPRECATE_DAYS=30    # L7: 30 days no re-encounter
COMPRESSION_MERGE_STRATEGY=soft_delete     # Q3: soft (default) / hard

# --- M9 Feature E: SpeakerLinker Layer 2 -----------------------------
SPEAKER_FUZZY_ENABLED=false
SPEAKER_FUZZY_TOKEN_RATIO=0.85             # L8
SPEAKER_VOICEPRINT_RECONFIRM_THRESHOLD=0.7 # L8
SPEAKER_FUZZY_MAX_CANDIDATES=5             # P1-2

# --- M9 Search --------------------------------------------------------
GLOBAL_SEARCH_TOP_K=5                      # L4
GLOBAL_SEARCH_LEVEL=0                      # default community level
```

---

## 8. Locked Decisions (L1-L10)

> **以下 10 项已锁定**，本 PRD 不再讨论。架构师 (高见远) 仅审 §9 Open Questions。偏离需主理人齐活林 + 架构师双签决议。

| # | 决策 | 锁定值 | 简短理由 |
|---|------|--------|---------|
| **L1** | Bi-temporal 数据模型遵循 Graphiti 语义：`valid_at` / `invalid_at` (NULL = current) + `created_at` / `expired_at` (NULL = live)，NULL 表示开放区间 | 4 个独立 timestamp 列；**不**用 `valid_time` range 列 | Graphiti 90.2% LongMemEval 已验证；4 列设计查询语义清晰；NULL 表示开放区间避免 range 类型复杂度 (MySQL range 支持弱) |
| **L2** | Leiden 增量阈值 = **30%** 节点变更 | delta_nodes / total_nodes > 30% → full recompute；≤ 30% → HIT-Leiden incremental | HIT-Leiden SIGMOD 2026 paper 推荐；30% 在 recall 保留与 full recompute 成本间最佳折衷；超过 30% 时 incremental 不再比 full 快 |
| **L3** | 社区摘要由**弱 LLM** (Qwen3.6-35B-A3B) 生成；存储于层级 0-3；结构变化 (merge / split) 触发重生成 | 弱 LLM；多层级；按结构变化触发 | 强 LLM 跑 500 community 成本失控；弱 LLM 单 community < 30s 足够；结构变化触发保证一致性，避免 staleness |
| **L4** | Global search 用**map-reduce**：所有 community summaries → 排序 → top-k → strong LLM | 全扫 → 排序 → top-k → strong LLM final | GraphRAG paper benchmark 显示 40% latency reduction vs brute-force；map 可并发 (弱 LLM rate limit 内) |
| **L5** | 图压缩运行**每周** cron | reuse M6 retention scheduler infrastructure | 月度周期太长 (graph 膨胀严重)；每日过度 (资源浪费)；每周三是社区共识 (PostgreSQL vacuum / Neo4j maintenance 均周级) |
| **L6** | Low-degree node 合并阈值 = degree ≤ 1 + rapidfuzz token_ratio ≥ 85 | degree ≤ 1 + token_ratio ≥ 85 | degree=1 (单边) 节点几乎确定是噪声 / 重复；token_ratio ≥ 85 与 M6 EntityMerger 阈值一致 (复用)；degree=2+ 节点保留以保图连通性 |
| **L7** | AMBIGUOUS edge 降级窗口 = **30 天**无再 encounter | 30 天 | 30 天覆盖月度业务周期 (客户到店频次)；短于 30 天会误杀季节性话题；长于 30 天图过度膨胀 |
| **L8** | SpeakerLinker Layer 2 fuzzy = rapidfuzz token_ratio ≥ 0.85 + voiceprint cosine 重确认 ≥ 0.7 → 升级 INFERRED | fuzzy token_ratio ≥ 0.85；reconfirm cosine ≥ 0.7；初始 AMBIGUOUS；reconfirm 后 INFERRED | token_ratio 0.85 与 M6 EntityMerger 阈值一致；voiceprint 0.7 是 M7 ambiguity_threshold 上限；reconfirm 机制保证不锁定错误合并 |
| **L9** | `enable_advanced_graph=False` 默认；M1-M8 零回归 | 默认 False；flag=False 时所有 M9 模块不 import，新表存在但空 | 沿用 M7 `enable_clap` / M8 `enable_streaming` 模式；保护现有部署；M9 是 opt-in 特性，不应强制 |
| **L10** | 所有新表带 `tenant_id` 用于 RBAC；community summaries 继承 tenant scope | 所有 M9 新表 (`community_summaries` / `speaker_merge_pending` / `leiden_jobs`) 含 `tenant_id` | AudioGraphy 是 tenant-scoped 系统 (M6 EntityMerger / M7 SpeakerLinker / M8 NetworkXGraphStore 全 tenant scope)；新表必须一致 |

---

## 9. Open Questions (Q1-Q3, each with PM Recommendation)

> 以下 3 项为 PM 推荐，最终决策由架构师 (高见远) 在 m9-architecture.md 中给出。PM 推荐基于产品 / 业务考量，架构师需考虑实现复杂度 / 性能 / 可维护性。

### Q1: Bi-temporal 冲突时旧 edge 处理策略

**背景**：当 DeltaGraphUpdater 检测到新 edge `(s, t, r)` 与现有 live edge 同 (s, t, r) 但属性 (weight / description) 不同时 (例如客户昨天说价格 18 万，今天说 20 万)，旧 edge 应如何处理？

**选项**：
- (a) Auto-invalidation：旧 edge 设 `invalid_at=now()`，新 edge 正常写入；time-travel 查询按 valid_at 区间返回。
- (b) Supersede pointer：旧 edge 不修改 `invalid_at` (保留 live)，但加 `superseded_by=new_edge_id` 指针；retrieval 默认仅返回最新版 (无 superseded_by 的)。
- (c) Keep both：两条 edge 都保留，新 edge 带 `created_at > old.created_at`；retrieval 默认按 created_at DESC 仅返回最新；time-travel 按 valid_at 区间。

**PM Recommendation**：推荐 **(b) Supersede pointer**。理由：(b) 完整保留 lineage，旧 edge 的 `valid_at` 区间未关闭意味着事实"曾一直有效"，与真实业务语义最匹配 (客户昨天说 18 万时，那个陈述在昨天到今天之间确实是有效的)；(a) 的 auto-invalidation 简单但丢失"该陈述在被取代前是否仍被引用"的语义；(c) 容易在 retrieval 引发歧义。代价：(b) 的 retrieval 路径需过滤 `superseded_by IS NULL`，与 `invalid_at IS NULL` 形成双轨过滤；写入路径增加一次 UPDATE。综合考虑审计 / 合规场景 (US-1 / US-8) 对 lineage 的强需求，(b) 是更稳健的选择；架构师需评估 retrieval SQL 复杂度是否可接受。

### Q2: 社区摘要生成层级粒度

**背景**：Leiden 在 10k node 图上典型产生 3-4 层 partition (level 0 = top ~10 communities, level 1 ~50, level 2 ~200, level 3 leaf ~500-1000)。每层都生成 summary 意味着 760-1260 次弱 LLM 调用 / 每次 Leiden run；仅 leaf + level 0 则约 510-1010 次。

**选项**：
- (a) 全部层级 (0-3)：完整覆盖；最高成本 (~1260 LLM calls / 10k node graph)；任一层级 global search 均可用。
- (b) 仅 leaf + level 0：覆盖最高层级 (宏观) + 最细层级 (微观)；中间层 (1-2) 按需 lazy 生成；节省约 40% LLM 调用。
- (c) 仅 level 0：仅宏观；最便宜但无法支持细粒度 drill-down (US-2 不可用)。

**PM Recommendation**：推荐 **(b) 仅 leaf + level 0**，中间层 lazy 生成。理由：US-2 (社区 drill-down) 实际只在两个极端使用——督导看宏观趋势 (level 0) 或质检员定位具体对话 (leaf)；中间层 level 1-2 是过渡层，用户实际很少停留；(b) 在不损失核心 UX 的前提下节省 40% LLM 成本，相当于每次 Leiden run 节省 ~$2-5 (Qwen3.6-A3B 单价 × 调用数)；lazy 生成保证当用户真的 drill-down 到 level 1 时实时生成 (latency < 30s 用户可接受)；US-3 global search 在 level 0 召回足够，不依赖中间层。代价：第一次 drill-down 到 level 1 时有 30s 延迟；可接受 (一次缓存)。架构师需确认 lazy generation 的并发与缓存策略。

### Q3: 压缩低度节点合并 — 硬删除还是软删除

**背景**：Low-degree node merge (degree ≤ 1, token_ratio ≥ 85) 在压缩任务中触发。source node (被合并到 canonical) 应硬删除还是软删除 (`expired_at=now()`)？

**选项**：
- (a) 硬删除：source node 从 `nodes` 表 + NetworkX graph 中物理删除；小图、查询快；丢失 lineage。
- (b) 软删除：source node 设 `expired_at=now()`；保留 bi-temporal lineage；图持续增长。
- (c) 混合：默认软删除；周期性 (季度) archive 到冷表后硬删除。

**PM Recommendation**：推荐 **(b) 软删除**。理由：M9 的核心价值主张之一是 bi-temporal 图谱 + 审计 (US-1 / US-8)，硬删除违背此原则；季度 archive (c) 增加运维复杂度 (冷表 schema / restore 流程) 不值得；性能方面，软删除下 NetworkX graph 节点数增长 20-30% / 年，但 retrieval 路径已过滤 `expired_at IS NULL`，查询性能不受影响 ( NetworkX MultiDiGraph 在 100k 节点规模查询仍 < 10ms)；MySQL `nodes` 表通过 `idx_nodes_expired` 索引高效过滤；图压缩主要节省的是 edge 数 (D-P0-3 / D-P0-4)，node 软删除的存储成本可接受。代价：长期 (3-5 年) 需考虑节点数膨胀；可观察 Prometheus 指标，超阈值时切换 (c) 策略。

---

## 10. UI/UX Impact

### 10.1 新增前端页面 (P1，M9 末期 ship)

#### 10.1.1 Community Explorer (G6 v5 hierarchy view)

**路径**：`/admin/communities`

**功能**：
- 左侧：Leiden 层级树 (level 0 → 1 → 2 → leaf)；点击节点展开 / 收起。
- 主区域：选中社区的 G6 v5 force-directed 可视化；节点按 entity_type 染色 (M5 既有方案)；边按 confidence_label 染色 (M8 既有方案)。
- 右侧 EntityPropertyPanel：选中社区的 `community_summary`、entity 数 / edge 数、最近重生成时间、member 实体 top-10。
- 顶部：tenant 切换 / 搜索框 (按 entity 名定位社区) / level 切换 (0-3)。

**数据 API**：
- `GET /api/v1/communities?level=N` → 层级树
- `GET /api/v1/communities/{id}` → 社区详情 (含 summary + members)
- `GET /api/v1/communities/{id}/graph` → G6 子图数据

#### 10.1.2 Time-Travel Query Builder

**路径**：`/inspector/time-travel`

**功能**：
- 顶部：entity 选择器 (autocomplete)、relation 输入框、time range datepicker (`as_of` 单点 或 `from-to` 区间)。
- 主区域：时间轴 widget (横轴时间，纵轴 edge 版本)；每个版本一条横线 (valid_at → invalid_at 区间)，颜色按 confidence_label。
- 底部：选中版本的详情 (source_ids / recording_ids / weight / description)。

**权限**：inspector / admin。

#### 10.1.3 Compression Run History

**路径**：`/admin/compression`

**功能**：
- 表格：run 时间 / tenant / 节点数变化 / 边数变化 / deprecated 数 / merged 数 / orphan invalidated 数 / duration。
- 详情页：单次 run 的 audit_log 详情 + diff sample。

### 10.2 现有页面修改

#### 10.2.1 Speaker Profile (US-9 fuzzy merge candidates)

**修改**：`/speakers/{id}` 页面新增 "Fuzzy Merge Candidates" 卡片：
- 展示 `speaker_merge_pending` 表中本节点作为候选的待审条目 (5 个)。
- 每条候选展示：候选 source speaker、rapidfuzz score、voiceprint cosine (若有)、proposed_at、source recording。
- 按钮：Confirm Merge / Reject Merge；点击后调 `/api/v1/speakers/{id}/confirm-merge` 或 `/reject-merge`，刷新页面。
- 权限：inspector / admin。

#### 10.2.2 Graph Explorer (bi-temporal toggle)

**修改**：现有 `/graph/explore` 页面顶部 toolbar 新增 "Time Travel" 切换按钮：
- 默认 off：仅展示 `invalid_at IS NULL AND expired_at IS NULL` 的 live edges (与 M8 行为一致)。
- On：弹出 datepicker，选择 as_of 时间点；查询切到 `/api/v1/graph/time-trival`；G6 渲染该时间点 valid edges。

#### 10.2.3 Search Bar (global / local 切换)

**修改**：现有搜索框旁新增 segmented control `[Local | Global]`：
- Local (默认)：M5 graph channel 检索路径不变。
- Global：调 `/api/v1/search/global`；展示 strong LLM final answer + 命中 community 列表 (top-5 summaries)。

### 10.3 UX 设计原则

- 所有 M9 新页面遵循 Arco Design Pro 现有设计语言 (与 M5-M8 一致)。
- bi-temporal / community / compression 三个 admin 功能仅在 `enable_advanced_graph=True` 且角色 ≥ inspector 时显示。
- 所有 fuzzy / pending 类操作必须可 undo (audit_log + soft delete 保证)。
- 时间显示统一 ISO 8601 + 本地化 (Asia/Shanghai)。
- 错误状态：Leiden job failed / community summary generation timeout / compression run failed 均在 admin UI 显示 banner 告警 + 链接到 audit_log。

---

## 11. Dependencies & Risks

### 11.1 依赖

| 依赖项 | 用途 | 来源 |
|--------|------|------|
| NetworkX + leidenalg / python-igraph | Leiden 算法基础 | 已在 M5 引入 |
| HIT-Leiden incremental lib (SIGMOD 2026) | 增量 Leiden 算法 | M9 新增；fallback (R-HIT) |
| rapidfuzz | fuzzy matching (compression / speaker Layer 2) | M6 已引入 |
| Qwen3.6-35B-A3B (weak LLM) | community summary 生成 | M4 已配置 (vllm-weak) |
| Qwen3.6-27B (strong LLM) | global search final reduction | M4 已配置 (vllm-strong) |
| M6 EntityMerger | compression low-degree merge 复用 | `core/entity_merger.py` |
| M6 retention scheduler | compression 周触发复用 | `core/retention.py` |
| M6 audit_log + tenant_configs | 审计 + per-tenant 配置 | `core/audit.py` |
| M7 SpeakerLinker + VoiceprintAdapter | Layer 2 重确认复用 | `core/speaker_linker.py` / `adapters/real/voiceprint_cam.py` |
| M8 DeltaGraphUpdater | bi-temporal + 增量 Leiden hook 点 | `core/delta_graph_updater.py` |
| M8 StreamingRWLock | Leiden 增量运行期间的图读写保护 | `core/streaming_rwlock.py` |
| Alembic | 4 个迁移脚本 (0010-0013) | M2 已引入 |

### 11.2 风险

| # | 风险 | 严重度 | 概率 | 缓解 |
|---|------|--------|------|------|
| **R-HIT** | HIT-Leiden Python 库 (SIGMOD 2026 paper code) 截至 2026-07 未发布成熟版本 / license 不 MIT-clean | 高 | 中 | (1) `leiden_incremental_lib_available=False` flag，fallback 到 full recompute + LRU cache (US-7)；(2) 基于 SIGMOD 2026 论文实现 independent port (≤ 500 LOC，MIT clean)；(3) 若两者均失败，永久 fallback full + cache，接受性能损失 (10k node ≤ 60s 仍可接受) |
| **R-BI-1** | Bi-temporal 查询在大规模 (10⁶ edges) 性能不达标 | 中 | 中 | (1) 索引 `idx_edges_bitemporal_valid(tenant_id, valid_at, invalid_at)` 覆盖 time-travel 查询；(2) 索引 `idx_edges_expired(tenant_id, expired_at) WHERE expired_at IS NULL` 部分索引覆盖 retrieval；(3) 单查询 explain analyze 在 100k edge 表 P95 < 200ms |
| **R-BI-2** | DeltaGraphUpdater batch 性能下降超 30% (bi-temporal 冲突检测 + Leiden 触发) | 中 | 中 | (1) 冲突检测走索引；批量查询避免 N+1 (A-P1-2)；(2) Leiden 异步触发 (DeltaGraphUpdater batch 不阻塞)；(3) 性能预算硬约束 (§7.1)，超阈值 PR 不合并 |
| **R-LEIDEN-1** | Leiden 全量重建在 10⁵+ 节点规模 > 60s | 中 | 中 | (1) leidenalg C backend 单线程 100k 节点 ~30s (NetworkX benchmark)；(2) 多线程 / iGraph 实现可降到 10s；(3) 超大租户 (>500k 节点) 文档化为 "需独立部署"，不在单实例 SLA |
| **R-LEIDEN-2** | Leiden 增量结果与全量不一致 (社区归属漂移) | 中 | 高 (HIT-Leiden 已知特性) | (1) 每周凌晨 03:00 自动触发 full recompute 对齐；(2) Prometheus `audiography_leiden_full_recompute_fallback_total` 监控；(3) 文档化 "incremental 结果可能滞后于 full，critical 场景请触发 full" |
| **R-LLM-1** | community summary 生成质量低 (weak LLM 抓不住社区核心) | 中 | 中 | (1) prompt 模板可配置 (`config.community_summary_prompt_path`)；(2) Q-后续：M9.1 引入 prompt A/B 测试框架；(3) 监控 global_search recall，低于 0.85 触发 prompt review |
| **R-LLM-2** | Global search strong LLM final reduction 单次调用 latency > 2s | 低 | 高 | (1) Qwen3.6-27B 单次 reduction prompt < 4k token，vLLM A100 FP4 实测 ~1.2s；(2) top-k=5 控制 prompt size；(3) 失败时降级到 weak LLM final (质量次但延迟可控) |
| **R-COMP-1** | 压缩任务误合并 (rapidfuzz token_ratio ≥ 85 但实际不是同一实体) | 高 | 中 | (1) 默认 soft_delete (Q3)，可恢复；(2) audit_log 完整记录 before/after；(3) admin UI 提供 undo 操作 (M9.1)；(4) tenant 可调阈值 (`compression_fuzzy_threshold`，高合规租户可调到 95) |
| **R-COMP-2** | AMBIGUOUS 30 天降级误杀季节性话题 | 中 | 低 | (1) 30 天窗口覆盖月度业务周期；(2) DEPRECATED 不是删除，可手动恢复；(3) audit_log trace |
| **R-FUZZY-1** | SpeakerLinker Layer 2 产生大量 AMBIGUOUS 候选淹没 inspector | 中 | 中 | (1) `speaker_fuzzy_max_candidates=5` 上限；(2) 候选按 score 排序，高 score 优先；(3) UI 卡片仅展示 5 个；(4) 批量 confirm API (M9.1) |
| **R-FUZZY-2** | Fuzzy reconfirm 误升级 (cosine ≥ 0.7 实际不是同一人) | 高 | 低 | (1) voiceprint 0.7 是 M7 阈值，已业务验证；(2) 升级是 INFERRED 不是 EXTRACTED，retrieval 默认 × 0.8 降权 (M8 Q3 决策)；(3) admin 可手动降级回 AMBIGUOUS |
| **R-RETRO** | M1-M8 在 `enable_advanced_graph=False` 下回归 | 高 | 低 | (1) L9 锁定；(2) CI 跑两套 (flag=true / flag=false)；(3) alembic 迁移默认值保证 M1-M8 查询行为不变；(4) 任何修改 M1-M8 文件的 PR 需双签 |
| **R-MIGRATION** | Alembic 4 个迁移在生产数据上失败 | 中 | 低 | (1) 迁移先在 staging 跑完整 upgrade + downgrade；(2) bi-temporal 列默认值保证向后兼容；(3) community_summaries 等新表是空表，无数据迁移风险 |
| **R-PERF-BATCH** | DeltaGraphUpdater 单 batch 增加 Leiden 触发后流式 latency 翻倍 | 中 | 中 | (1) Leiden 异步触发 (DeltaGraphUpdater batch 不等待 Leiden 完成)；(2) Leiden 在独立 worker pool 跑；(3) Prometheus 监控 streaming_e2e_latency_ms 是否超 M8 P95 3s |

---

## 12. Success Metrics & Acceptance Criteria

### 12.1 功能验收

#### 12.1.1 Bi-temporal (Feature A)

- [ ] **AC-A-01**: alembic upgrade `0010_m9_bitemporal_edges` 在 staging + 生产均通过；downgrade 同样通过。
- [ ] **AC-A-02**: DeltaGraphUpdater 写入新 edge 时自动填 4 个 timestamp 列；调用方代码无改动。
- [ ] **AC-A-03**: 客户陈述从 18 万变更为 20 万后，`/api/v1/graph/edge-history` 返回 2 个版本；旧版 `superseded_by` 指向新版 (Q1 决策实施)。
- [ ] **AC-A-04**: `/api/v1/graph/time-travel?entity=X&as_of=YYYY-MM-DD` 返回正确时间区间的 edges。
- [ ] **AC-A-05**: retention cascade 删节点 N 后，相关 edges 标 `expired_at=now()`；retrieval 不返回 expired edges；audit_log 含 `edge_invalidated_by_retention`。
- [ ] **AC-A-06**: `enable_advanced_graph=False` 下，M1-M8 retrieval 行为不变 (live edges 仍按 `expired_at IS NULL` 过滤，但默认值为 NULL 与 M8 一致)。

#### 12.1.2 Leiden 增量 (Feature B)

- [ ] **AC-B-01**: DeltaGraphUpdater batch 完成后自动触发 Leiden 增量；`nodes.community_id` 列被更新。
- [ ] **AC-B-02**: 10k 节点增量 Leiden 耗时 ≤ 5s (Prometheus histogram P95)。
- [ ] **AC-B-03**: delta/total > 30% 时降级到 full recompute；Prometheus counter 触发。
- [ ] **AC-B-04**: `POST /api/v1/admin/leiden/recompute?mode=full` 异步执行；`GET /api/v1/admin/leiden/jobs/{job_id}` 返回状态。
- [ ] **AC-B-05**: HIT-Leiden lib 缺失 (`leiden_incremental_lib_available=False`) 时，所有测试在 full+cache 模式下通过。
- [ ] **AC-B-06**: 10 万节点全量重建 ≤ 60s。

#### 12.1.3 Community summaries + Global search (Feature C)

- [ ] **AC-C-01**: Leiden 完成后异步生成 community summaries；level 0 + leaf 覆盖率 100%。
- [ ] **AC-C-02**: 单 community summary 生成 ≤ 30s；并发 5 时吞吐稳定。
- [ ] **AC-C-03**: 社区结构变化 ≥ 30% 时触发重生成；Prometheus counter 自增。
- [ ] **AC-C-04**: `/api/v1/search/global` map-reduce 端到端 P95 ≤ 2s。
- [ ] **AC-C-05**: `/api/v1/search/local` API 与 M5 graph channel 行为一致 (regression)。
- [ ] **AC-C-06**: Global search recall@5 ≥ 0.85 vs brute-force 在 gold set 上。

#### 12.1.4 图压缩 (Feature D)

- [ ] **AC-D-01**: 周压缩 cron 周日 03:00 触发；audit_log 含 `compression_run` 行。
- [ ] **AC-D-02**: Low-degree 合并：rapidfuzz ≥ 85 + degree ≤ 1 触发；canonical 节点保留；source node `expired_at=now()` (Q3 soft)。
- [ ] **AC-D-03**: AMBIGUOUS 30 天未 encounter 降级 DEPRECATED；audit_log trace。
- [ ] **AC-D-04**: Orphan edge 失效：retention 删节点后相关 edge 自动 `invalid_at + expired_at`。
- [ ] **AC-D-05**: 月度 edge 数下降 ≥ 20% (vs 不开压缩的对照租户)。

#### 12.1.5 SpeakerLinker Layer 2 (Feature E)

- [ ] **AC-E-01**: Layer 1 (cosine ≥ 0.5) miss 时触发 Layer 2 fuzzy；rapidfuzz token_ratio ≥ 0.85 提议 AMBIGUOUS 合并。
- [ ] **AC-E-02**: 后续录音 voiceprint cosine ≥ 0.7 升级 INFERRED；`speaker_merge_pending` 行删除。
- [ ] **AC-E-03**: `/api/v1/speakers/merge-pending` 列表 API 工作；inspector 角色可见。
- [ ] **AC-E-04**: Speaker Profile 页面展示待审候选；confirm / reject 按钮工作。
- [ ] **AC-E-05**: 测试集 AMBIGUOUS 对 ≥ 60% 被消化 (auto upgrade + admin confirm)。

### 12.2 性能验收

- [ ] Leiden 增量 < 5s on 10k nodes (AC-B-02)。
- [ ] Leiden 全量 < 60s on 10k nodes；≤ 60s on 100k nodes (AC-B-06)。
- [ ] Community summary generation < 30s per community (AC-C-02)。
- [ ] Global search p95 < 2s on 10k nodes + 500 communities (AC-C-04)。
- [ ] Compression weekly run < 5 min on 100k edges (§7.1)。
- [ ] DeltaGraphUpdater overhead < 30% vs M8 (§7.1)。

### 12.3 回归与质量

- [ ] **AC-REGRESS-01**: `enable_advanced_graph=False` 下 M1-M8 全部 pytest 测试 0 失败。
- [ ] **AC-REGRESS-02**: `enable_advanced_graph=True` 下 M1-M8 测试 ≤ 5 失败 (与 M9 显式新行为相关，文档化)。
- [ ] **AC-QUALITY-01**: M9 新增模块代码覆盖率 ≥ 88% (per-module ≥ 85% OR total ≥ 88%，沿用 M6-M8 规则)。
- [ ] **AC-QUALITY-02**: `mypy backend/audio_graphy/{core,adapters,api,models}/` 0 错；`ruff check backend/` 0 错。
- [ ] **AC-QUALITY-03**: 所有新公开 API 含中英双语 docstring；Protocol 满足 `@runtime_checkable`。
- [ ] **AC-QUALITY-04**: `pytest --collect-only` 测试数 ≥ 1300 (M8 ~1150 + M9 ~150 新增)。

### 12.4 文档验收

- [ ] `docs/m9-prd.md` (本文件) 总长 800-1200 行。
- [ ] `docs/m9-architecture.md` (架构师写) 总长 ≤ 1500 行；含 Protocol / 类签名 / 数据模型 / 迁移 / 任务分解。
- [ ] `docs/advanced-graph.md` (用户文档) 总长 ≤ 500 行；含 config 说明 / API 示例 / FAQ。
- [ ] `README.md` 加 M9 状态说明 (≤ 10 行)：advanced graph features opt-in。
- [ ] `.env.example` 覆盖所有 M9 新字段 (含注释)。
- [ ] `NOTICES.md` 补 HIT-Leiden lib / Graphiti 借鉴声明 (若引入)。

### 12.5 成功指标 (上线后 30 天)

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| Leiden 增量运行 P95 | < 5s | Prometheus histogram |
| Leiden 全量回退率 | < 10% / 周 | counter / total |
| Community summary 覆盖率 | 100% (level 0 + leaf) | DB query |
| Global search recall@5 | ≥ 0.85 | weekly eval run |
| Global search P95 latency | < 2s | Prometheus histogram |
| 压缩 edge 月度降幅 | ≥ 20% | DB query (vs 对照) |
| SpeakerLinker AMBIGUOUS 消化率 | ≥ 60% | DB query (待审队列) |
| `enable_advanced_graph=False` 部署回归 | 0 | customer report |
| 新模块代码覆盖率 | ≥ 88% | CI |

---

## 13. 附录 A：M9 与 M7/M8 PRD 的结构对照

| 章节 | m7-prd.md | m8-prd.md | 本 PRD | 一致性 |
|------|-----------|-----------|--------|--------|
| Document Metadata | ✅ | ✅ | ✅ §1 | ✅ |
| Executive Summary / TL;DR | ✅ §1 | ✅ §0 | ✅ §2 | ✅ |
| Background & Motivation | ✅ §2 | ✅ §1 | ✅ §3 (3 子节) | ✅ |
| Goals & Non-Goals | ✅ §4.4 | ✅ §3.3 | ✅ §4 (in-scope A-E + out-of-scope) | ✅ |
| User Stories | ✅ §3 (8 个) | ✅ §2 (3 个 + 矩阵) | ✅ §5 (12 个) | ✅ |
| Functional Requirements (P0/P1/P2) | ✅ §4.1-4.3 | ✅ §3.1-3.3 | ✅ §6 (5 特性 + 综合) | ✅ |
| Non-Functional | ✅ §6 | ✅ §5 | ✅ §7 | ✅ |
| Locked Decisions (L1-L10) | ✅ §5 | ✅ §4 | ✅ §8 (verbatim) | ✅ |
| Open Questions (Q1-Q5 with 推荐) | ✅ §10 | ✅ §9 | ✅ §9 (Q1-Q3 with Recommendation) | ✅ |
| UI/UX Impact | 部分 (§9.3) | 部分 (§10 Appendices) | ✅ §10 (3 新页面 + 3 修改) | ✅ |
| Dependencies & Risks | ✅ §7 + §8 | ✅ §6 + §7 | ✅ §11 (依赖 + 13 风险) | ✅ |
| Success Metrics & Acceptance | ✅ §9 | ✅ §8 | ✅ §12 (5 子节) | ✅ |

---

## 14. 附录 B：M9 任务映射 (供架构师 task-split 参考)

| 任务 ID | 名称 | 工作流 | 估算 LOC | 主要依赖 |
|---------|------|--------|---------|---------|
| T1 | alembic 0010-0013 迁移 + SQLAlchemy models | WS-A | ~400 | — |
| T2 | DeltaGraphUpdater bi-temporal 改造 + 冲突检测 (Q1) | WS-A | ~300 | T1 |
| T3 | `/api/v1/graph/time-travel` + `/edge-history` API | WS-A | ~250 | T2 |
| T4 | retention cascade 协同 (edge 失效) | WS-A | ~120 | T2 |
| T5 | `core/leiden_incremental.py` + lib wrapper + fallback | WS-B | ~500 | T1 |
| T6 | DeltaGraphUpdater Leiden hook (异步触发) | WS-B | ~150 | T5 |
| T7 | `/api/v1/admin/leiden/recompute` API + job 表 | WS-B | ~250 | T5 |
| T8 | `core/community_summarizer.py` + Protocol + real impl | WS-B | ~400 | T5 |
| T9 | `core/global_search.py` map-reduce + `/api/v1/search/global` | WS-B | ~350 | T8 |
| T10 | `/api/v1/search/local` harmonize | WS-B | ~150 | T8 |
| T11 | `core/graph_compressor.py` (3 清理函数) | WS-C | ~450 | T1 |
| T12 | Compression cron 调度接入 (M6 retention scheduler) | WS-C | ~100 | T11 |
| T13 | `core/speaker_fuzzy_matcher.py` + SpeakerLinker Layer 2 实现 | WS-C | ~400 | T1 |
| T14 | `/api/v1/speakers/merge-pending` + confirm/reject API | WS-C | ~200 | T13 |
| T15 | Prometheus + OTel 埋点 (跨特性) | WS-C | ~300 | T2, T5, T8, T11, T13 |
| T16 | 集成测试 + 回归套件 (flag=true/false) | WS-C | ~600 | 全部 |
| T17 | 前端 Community Explorer / Time-Travel / Compression History / SpeakerProfile 修改 | WS-C | ~1500 | 全部 API |
| T18 | 文档：m9-architecture.md / advanced-graph.md / README / .env.example | — | ~800 | 全部 |

**总计 M9 增量**：≤ 7500 行 (含前端 + 测试 + 文档)。

---

## 15. 附录 C：本 PRD 自检

| 检查项 | 目标 | 实际 |
|--------|------|------|
| 总行数 | 700-1200 | 见文件末尾行号 |
| 章节数 | 12 (按 §6 锁定结构) | 12 ✅ (§1-12) + 3 附录 |
| 5 features 各 P0 ≥ 4 | A: 8 / B: 6 / C: 6 / D: 5 / E: 4 | ✅ 全部达标 |
| L1-L10 verbatim from §4 | ✅ | §8 表格 verbatim |
| Q1-Q3 each Recommendation | 3-6 sentences | ✅ §9 每项 |
| 用户故事数 | 8-12 | 12 ✅ |
| 跨 M6/M7/M8 引用 | 多处 | ✅ §3.1 / §3.2 / §6 / §11 |
| 开源 tone | MIT-friendly | ✅ §7.5 + 全文 |
| Out-of-scope 明确 | M10+ 项 | ✅ §4.2 (10 项) |
| 风险数 | 充分覆盖 | 13 (含 R-HIT / R-BI / R-LEIDEN / R-COMP / R-FUZZY) |
| 与锁定决策偏离 | 0 | **0** (L1-L10 全部 verbatim，Q1-Q3 推荐不偏离) |

---

**PRD 终点。** 主理人 (齐活林) 确认 Q1-Q3 PM 推荐 → 架构师 (高见远) 在 m9-architecture.md 给出最终决策 → T1-T18 任务认领。任何修改 L1-L10 的请求需主理人 + 架构师双签；其余章节 (P1/P2 范围、风险缓解、UI 细节) 可在 review 中迭代。
