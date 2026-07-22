# AudioGraphy M8 PRD — Phase 4 Streaming Extension

**Milestone:** M8 · Phase 4 流式扩展 / Streaming Extension
**Author:** 许清楚（Xu Qingchu）· 产品经理
**Status:** Draft v1.0 — Pending 架构师 review
**Last updated:** 2026-07-22
**Target line range:** 800–1200（本文件实际行数见文末 Appendix C 自检）
**Depends on:** M6 (EntityMerger) · M7 (SpeakerLinker / VoiceprintAdapter / AudioEmbedAdapter)
**Predecessor:** `docs/m7-prd.md`（PRD 风格与结构模板）

---

## 0. TL;DR

M8 将 AudioGraphy 从**纯批处理**系统扩展为**批 + 流**双模态系统，新增 `WebSocket /ws/stream` 端点，与现有 REST API 共存（L1）。流式链路复用 M1–M7 的批处理内核（VAD → ASR → Chunker → Extractor → Merger → Linker），但在两端替换为有状态的流式适配器：

- **入口侧**：Silero VAD 流式（L3，512 采样/块，32ms@16kHz，4-态状态机）+ funASR `paraformer-zh-streaming` 流式（L2，WebSocket:10095，chunk_size=[5,10,5]，600ms lookahead）。
- **出口侧**：增量图谱更新（L5），复用 M6 `EntityMerger` 与 M7 `SpeakerLinker`，**不引入 bi-temporal**；社区检测（Leiden）**不**做增量，由 admin API 触发全量重建（L6）。
- **文本状态**：双态（confirmed + realtime），仅 confirmed 段进入图谱；realtime 仅用于前端实时展示（L4）。
- **标签/打标**：每 N=5 个 confirmed 段触发一次批量化打标（L7），**不**实现真正的 token-by-token 流式 LLM。
- **GraphRAG delta**：通过 content-hash 比对，仅处理新增/修改文档（L8）。
- **边置信度**：保留 Graphify 的 `EXTRACTED / INFERRED / AMBIGUOUS` 三标签（L9）。
- **向后兼容**：M1–M7 批处理路径**不动**，流式为纯增量模块（L10）。

**P0 交付**：可工作的 `/ws/stream` 端到端流式管线 + 门店实时质检场景验收通过。
**P1 交付**：管理后台触发 Leiden 全量重建 + 增量打标 + 流式 SpeakerFuzzyMatcher。
**P2 交付**：流式 RWLock 快照 + 双速索引 + Compaction（DESIGN §9.2 长期项）。

---

## 1. Background

### 1.1 项目坐标

AudioGraphy 是开源（MIT）门店录音图谱 RAG 系统，继承 VideoRAG 的图谱内核（GraphRAG prompts、LightRAG/nano-graphrag 增量合并）。

**四阶段路线图：**

| Phase | 里程碑 | 状态 | 主题 |
|---|---|---|---|
| 1 | M1–M5 | ✅ shipped | 文本图谱 RAG（批处理内核） |
| 2 | M7 | ✅ shipped | 音频嵌入 + 说话人节点（VoiceprintAdapter、AudioEmbedAdapter、SpeakerLinker） |
| 3 | M6 | ✅ shipped | 治理（EntityMerger、tag 三层模型、DSAR、审计） |
| 4 | **M8** | 🚧 this PRD | **流式扩展（streaming ASR + streaming VAD + 增量图谱）** |

### 1.2 为什么需要 M8

M1–M7 的批处理模型在「门店录音 → 夜间离线入库 → 次日检索」场景下表现良好，但**无法支撑实时业务**：

1. **门店实时质检**：督导岗需要在客户对话进行中即可看到风险词触发、话术偏离、情绪预警，事后批处理无法满足 SLA。
2. **现场干预**：当客户表达退订/投诉倾向时，门店经理需要在 30s 内收到提示——批处理的 4–8h 延迟无法接受。
3. **长录音内存压力**：超过 2h 的录音，当前 `core/chunker.py` 全量加载 + 生命周期 flush 模型内存峰值 ≥ 8GB，流式分块天然解决该问题（同时部分回应了 `post-m5-gap-audit.md` §7.5 的并发安全遗留）。

### 1.3 为什么 VideoRAG 不直接支持流式

详见 `DESIGN.md` §9.1。核心原因：VideoRAG 的图谱构建假设**全量文档可见**——社区检测（Leiden）需要全局边权分布，实体抽取依赖上下文窗口看到完整段落。直接流式化 VideoRAG 会破坏这些不变量。AudioGraphy 的策略是**复用 GraphRAG 的增量 entity merge**（DESIGN §9 明确指出："GraphRAG's incremental entity merge is streaming-friendly — the correct foundation for streaming"），并在社区检测层退化为**周期性全量重建**（L6），以换取流式可用性。

### 1.4 当前代码盘点（M8 的脚手架已就绪）

| 模块 | 路径 | M8 影响 |
|---|---|---|
| 批处理内核 | `core/chunker.py` (523 行)、`core/extractor.py` (721 行) | 不动；流式新增 `core/streaming/` 目录（`post-m5-gap-audit.md` §9 显示该目录当前为空） |
| Protocol 层 | `adapters/protocols.py` (278 行) | 新增 `StreamingVADAdapter` + `StreamingASRAdapter` 两个 Protocol |
| VAD 批适配器 | `adapters/real/vad_silero.py` (200 行) | 不动；新增 `adapters/real/streaming_vad_silero.py` |
| ASR 批适配器 | `adapters/real/funasr.py` (276 行) | 不动；新增 `adapters/real/streaming_funasr.py`（HTTP → WebSocket:10095） |
| 实体合并 | `core/entity_merger.py` (375 行) | **复用，不修改**（M6 已设计为可重入、tenant-scoped） |
| 说话人链接 | `core/speaker_linker.py` (617 行) | **复用 `run()`，新增 `SpeakerFuzzyMatcher`**（M7 在 docstring 第 36–40 行已明确承诺 M8 实现） |
| REST API | `api/*.py`（14 个 router） | 不动；新增 `api/streaming.py` 暴露 `/ws/stream` |
| 治理 | `core/audit.py`、`core/crypto.py` | 复用；voiceprint 仍走 AudioCrypto 加密 |

---

## 2. User Stories

### 2.1 主场景：门店实时质检（P0 必交付）

**Persona**：王督导，连锁餐饮品牌区域督导，负责 12 家门店的客户服务质检。

**当前痛点**：
- 现行流程：门店录音 → 当晚 23:00 自动上传 → 次日 09:00 看到质检报告。客户投诉往往已经发酵到社交媒体。
- 王督导希望在客户对话进行中即可在仪表盘看到：当前对话的话题分类、是否触发违规话术、客户情绪曲线。

**M8 验收场景**：

1. 门店设备通过浏览器 WebSocket 连接到 `/ws/stream`，麦克风音频以 16kHz PCM 实时推送。
2. 系统在前 5s 内完成首段 VAD 检测，前端开始展示实时转写（realtime 文本）。
3. 每 10–15s 产生一段 confirmed 段，进入图谱增量更新；前端收到 `event=segment_confirmed` 消息。
4. 当连续 5 段 confirmed 触发后（约 60–90s），系统批量打标（违规话术、话题分类），前端收到 `event=tags_updated`。
5. 王督导在仪表盘的颜色编码实时变化：绿色（正常）→ 黄色（话术偏离）→ 红色（违规触发）。
6. 对话结束后 30s 内，最终图谱版本固化，可查询完整段、实体、关系。

**SLA**：
- 端到端延迟（麦克风 → 前端收到 realtime 文本）：P50 ≤ 1.5s，P95 ≤ 3s。
- Confirmed 段产生延迟：≤ 8s（受 funASR chunk_size 与 VAD min_speech_sec 共同决定）。
- 单 WebSocket 会话稳定运行 ≥ 2h 不掉线（含服务端优雅重连）。

### 2.2 次场景：现场干预（P1）

**Persona**：李经理，单店店长，希望在客户表达退订/投诉倾向时 30s 内收到企微推送。

**M8 验收**：
- 流式段进入图谱后，若增量抽取识别到 `投诉意图` 实体或 `退订` 关系，触发 webhook（M8 复用 M6 的 `webhook` 表，新增事件类型 `streaming_alert`）。
- webhook 投递延迟 ≤ 30s（从 confirmed 段到企微消息送达）。
- 单次告警去重窗口：5 分钟（同一 `recording_id` + 同一告警类型不重复推送）。

### 2.3 辅助场景：长录音降内存（P2）

**Persona**：赵运维，负责 AudioGraphy 集群运维。

**当前痛点**：2h 以上录音的批处理 OOM，被迫手动切分上传。

**M8 验收**：
- 长录音可改走流式管线（即使不需要实时展示），单会话内存峰值 ≤ 1.5GB。
- 流式产出的图谱与等价批处理产出的图谱，实体集合 Jaccard 相似度 ≥ 0.92（允许流式因 VAD 切分差异略有不同）。

### 2.4 验收矩阵（Acceptance Matrix）

| US | 场景 | 优先级 | P0 验收项 | P1 验收项 | P2 验收项 |
|---|---|---|---|---|---|
| 2.1 | 门店实时质检 | **P0** | 端到端流式 + 实时转写 + 增量图谱 | — | — |
| 2.2 | 现场干预 | P1 | — | webhook + 去重 | — |
| 2.3 | 长录音降内存 | P2 | — | — | 流式低内存模式 |
| 2.5* | 管理员重建社区 | P1 | — | `POST /api/v1/graph/rebuild` | — |
| 2.6* | 模糊说话人匹配 | P1 | — | SpeakerFuzzyMatcher | — |

---

## 3. Scope — P0 / P1 / P2

### 3.1 P0（M8 必须交付）

| ID | 名称 | 描述 | 验收 |
|---|---|---|---|
| P0-1 | `StreamingVADAdapter` Protocol | 新增 Protocol（`adapters/protocols.py`），签名 `async def push_chunk(pcm: bytes) -> VADChunkResult`；`async def finalize() -> tuple[SegmentRecord, ...]` | Protocol 通过 `@runtime_checkable`；示例实现 `StreamingSileroVADAdapter` 通过 `_STREAMING_VAD_PROTOCOL_CHECK` |
| P0-2 | `StreamingASRAdapter` Protocol | 新增 Protocol，签名 `async def push_pcm(pcm: bytes) -> ASRDeltaResult`；`async def finalize() -> ASRResult` | 同上；示例实现 `StreamingFunASRAdapter` 走 WebSocket:10095 |
| P0-3 | `SessionState` 数据结构 | 每个 WebSocket 连接持有一个 `SessionState`：包含 VAD 隐状态、ASR 缓存、pending segments 队列 | 数据类定义 + 单测覆盖生命周期（创建/恢复/销毁） |
| P0-4 | `/ws/stream` 端点 | 新增 `api/streaming.py`，FastAPI WebSocket 路由；接收 PCM chunks，下发 5 类事件 | 集成测试：模拟 60s 音频流，断言事件序列与最终图谱 |
| P0-5 | Dual-state text 协议 | confirmed（进入图谱）+ realtime（仅前端展示） | 协议 schema 见 Appendix A；前端 mock 验收 |
| P0-6 | Delta-merge 图谱更新 | confirmed 段批量调用 `EntityExtractor` + `EntityMerger.merge()` + `SpeakerLinker.run()` | 复用 M6/M7 内核，**不**改 EntityMerger / SpeakerLinker 源码 |
| P0-7 | Content-hash delta 检测 | 仅当 batch 内 chunk 的 content-hash 不在图谱中时才触发抽取 | 复用 `ChunkRecord.content_hash` 字段（chunker.py 已有） |
| P0-8 | Edge confidence 标签 | 实体-关系边携带 `EXTRACTED / INFERRED / AMBIGUOUS` | 数据库迁移 + 抽取 prompt 调整（Graphify 风格） |
| P0-9 | 门店实时质检端到端 demo | 浏览器麦克风 → /ws/stream → 前端仪表盘 | 王督导场景（US 2.1）SLA 达标 |
| P0-10 | 数据库 schema 迁移 | `edges.confidence_tag`（VARCHAR(16)，默认 `EXTRACTED`）；`edges.streaming_origin`（BOOL，默认 FALSE）；`streaming_sessions` 新表 | Alembic 迁移脚本 + 回滚脚本 |
| P0-11 | `streaming_sessions` 表 | 记录每个 WebSocket 会话：`session_id`、`recording_id`、`tenant_id`、`started_at`、`ended_at`、`seg_confirmed_count`、`seg_realtime_count`、`bytes_in`、`error_count` | 表 + CRUD + 单测 |
| P0-12 | Prometheus 指标埋点 | §5.4 列出的所有 metrics 通过 `prom_client` 暴露在 `/metrics` | 压测时 metrics 可观测 |
| P0-13 | OpenTelemetry span 链 | `ws_recv → vad → asr → extractor → merger → db_write` 全链路 trace | trace_id 通过首帧下发；前端可关联 |

### 3.1.1 P0 任务分解（供架构师 task-split 参考）

按依赖顺序（T1 必须先于 T2 完成）：

- **T1 — Schema 迁移**（P0-10）：Alembic `migrations/versions/xxxx_add_streaming_edges.py`。
- **T2 — Protocol 定义**（P0-1、P0-2）：`adapters/protocols.py` 追加两个 `@runtime_checkable` Protocol；只追加不修改既有 6 个。
- **T3 — StreamingSileroVADAdapter**（依赖 T2）：4-态 FSM + 隐状态对象；单测覆盖每个状态转移。
- **T4 — StreamingFunASRAdapter**（依赖 T2）：WebSocket 客户端 + 异常映射；mock funASR server 单测。
- **T5 — SessionState**（依赖 T3、T4）：组合 VAD + ASR + pending 队列；提供 `on_pcm_chunk()` / `on_finalize()` / `to_redis()` / `from_redis()`。
- **T6 — Delta-detector**（P0-7、P0-8）：content-hash 集合查询；新增边时打 `EXTRACTED/INFERRED/AMBIGUOUS` 标签。
- **T7 — /ws/stream 端点**（依赖 T5、T6）：FastAPI WebSocket；接入 auth/tenant/audit 中间件；5 类事件下发。
- **T8 — 增量图谱更新**（P0-6）：复用 `EntityExtractor.extract_from_chunks()` 的兄弟方法 `extract_from_streaming_batch()`，**不**改既有方法。
- **T9 — Prometheus + OTel 埋点**（P0-12、P0-13）：在 T3–T7 关键路径插入埋点。
- **T10 — 集成测试**：mock 60s 音频流；断言事件序列 + 最终图谱。
- **T11 — 端到端 demo**（P0-9）：前端 mock + 真实麦克风；SLA 校验。
- **T12 — 批处理回归套件**：CI 跑 M1–M7 eval，确认零回归。

### 3.2 P1（M8 应当交付，缺失可降级到 P2）

| ID | 名称 | 描述 |
|---|---|---|
| P1-1 | Admin 全量社区重建 API | `POST /api/v1/graph/rebuild`：触发 Leiden 全量重算，返回 job_id；`GET /api/v1/graph/rebuild/{job_id}` 查询状态 |
| P1-2 | 批量打标 N=5 | 每 5 个 confirmed 段触发一次 LLM 标签批处理；标签写入三层模型（tag_facts / tag_current / tag_stats） |
| P1-3 | SpeakerFuzzyMatcher | 新增 `core/speaker_fuzzy_matcher.py`：rapidfuzz 在 speaker display_name 上的模糊匹配（M7 speaker_linker.py 第 36–40 行承诺的 M8 交付项） |
| P1-4 | 流式 webhook | 复用 M6 webhook 表，新增事件 `streaming_alert`；5 分钟去重窗口 |
| P1-5 | WebSocket 重连 | 客户端断线后 5s 内重连，服务端保留 SessionState 60s（session_id 恢复） |

### 3.3 P2（长期项，可推迟到 M8.1）

| ID | 名称 | 描述 |
|---|---|---|
| P2-1 | RWLock 快照 | 图谱读写锁，流式写入与批查询并发安全（DESIGN §9.2） |
| P2-2 | 双速索引 | 热数据（最近 24h）走内存索引，冷数据走磁盘（DESIGN §9.2） |
| P2-3 | Compaction | 长会话的图谱版本合并（避免增量导致版本爆炸） |
| P2-4 | 流式社区检测实验 | 尝试 incremental Leiden（如 `incremental-leiden` 库）；仅实验，不进主干 |

---

## 4. Locked Decisions（L1–L10）

> **以下 10 条决策已锁定，不允许在本 PRD 内偏离。** 任何偏离需主理人齐活林 + 架构师 双签决议。

| ID | 决策 | 理由 | 影响模块 |
|---|---|---|---|
| **L1** | WebSocket 端点 `/ws/stream` 与现有 REST API **共存**；不替换、不重写 REST 路径 | M1–M7 的 REST API 已被前端、运维脚本、eval harness 大量调用；共存保证零回归 | `api/streaming.py` 新增；`api/*.py` 14 个 router 不动 |
| **L2** | 流式 ASR 使用 funASR `paraformer-zh-streaming`，通过 **WebSocket:10095** 接入；`chunk_size=[5,10,5]`（600ms lookahead，24 帧累积） | funASR 官方流式协议；600ms lookahead 在 CER（≤ 8% on AISHELL-2）与延迟间最佳折衷；与批处理 `paraformer-zh`（HTTP）解耦 | 新增 `StreamingFunASRAdapter`；批处理 `FunASRAdapter`（HTTP）保留 |
| **L3** | 流式 VAD 使用 Silero VAD streaming；**512 采样/块（32ms@16kHz）**；4-态状态机：`silence → pendingSpeech → speech → pendingSilence`；阈值 `onset=0.5 / offset=0.35 / min_speech=0.25s / min_silence=0.10s`；LSTM 隐状态跨块保留 | 32ms 块为 Silero 官方推荐；onset/offset 滞回避免抖动；隐状态必须跨块（不可跳块） | 新增 `StreamingSileroVADAdapter`；批处理 `SileroVADAdapter`（HTTP `/v1/vad/segment`）保留 |
| **L4** | **双态文本**：`confirmed`（funASR 句终确定）+ `realtime`（中间增量）；**仅 confirmed 进入图谱**，realtime 仅用于前端展示 | 避免半截句子污染图谱实体（如 "我想退..." → 抽取出 `退订` 实体是错误的）；前端用户体验仍保留"打字机效果" | `SessionState.pending_realtime` 不进 EntityMerger |
| **L5** | 增量图谱更新**复用** M6 `EntityMerger` + M7 `SpeakerLinker`；**不引入 bi-temporal**（不记录历史版本） | EntityMerger 已是 tenant-scoped、可重入、带 fuzzy 容错；SpeakerLinker 已支持 voiceprint + 手动确认；bi-temporal 显著增加 schema 复杂度，M8 不值得 | `EntityMerger`、`SpeakerLinker` 源码不改 |
| **L6** | 社区检测（Leiden）**不**做增量；admin API `POST /api/v1/graph/rebuild` 触发全量重建 | Leiden 对全局边权分布敏感，流式边插入会破坏社区质量；全量重建虽慢（分钟级）但可异步、可定期调度 | `api/graph.py` 新增 rebuild endpoint；`core/community.py` 不改增量接口 |
| **L7** | 流式标签/打标：**每 N=5 个 confirmed 段触发一次批 LLM 调用**，**不**做 token-by-token 流式 LLM | 真正的流式 LLM（如 GPT-4 streaming）对图谱 RAG 无意义——图谱实体需要完整 prompt 上下文；批量化 N=5 在延迟（≤ 90s）与 LLM 成本间最佳 | 新增 `core/streaming/batch_tagger.py`；复用 M6 三层 tag 模型 |
| **L8** | GraphRAG delta 检测：通过 **content-hash 比对**，仅处理新增/修改文档 | 复用现有 `ChunkRecord.content_hash` 字段（chunker.py:67 已有）；O(1) hash 查找替代 O(N) 文本比较 | `core/streaming/delta_detector.py` |
| **L9** | 边置信度标签：`EXTRACTED`（LLM 直接抽取）/ `INFERRED`（Merger 合并产生）/ `AMBIGUOUS`（rapidfuzz 模糊或 cosine 模糊） | 继承 Graphify 设计；支撑下游 RAG 检索时的"可信度过滤" | 数据库迁移 `edges.confidence_tag` 列；抽取 prompt 调整 |
| **L10** | **向后兼容**：M1–M7 批处理路径**不动**；流式为纯增量模块（新增 `core/streaming/`、`adapters/real/streaming_*.py`、`api/streaming.py`） | M1–M7 已上线，回归风险高于流式本身的收益；纯增量保证回滚成本最低 | 所有新增代码隔离在新目录/新文件 |

---

## 5. Non-Functional Requirements

### 5.1 性能

| 指标 | 目标值 | 测量方式 |
|---|---|---|
| 端到端延迟（mic → frontend realtime text） | P50 ≤ 1.5s，P95 ≤ 3s | Prometheus histogram `streaming_e2e_latency_ms` |
| Confirmed 段产生延迟 | ≤ 8s | 从 VAD 检测到 speech onset 计起 |
| 单 WebSocket 会话并发 CPU | ≤ 1 core（含 VAD + ASR + 抽取） | `docker stats` 验证 |
| 单会话内存峰值 | ≤ 1.5GB | US 2.3 长录音场景 |
| 同时在线会话数 | ≥ 50（单节点，4 core / 16GB） | 压测：locust + fake audio |
| 增量抽取触发到图谱可查 | ≤ 15s | 从 confirmed 段产生到 `GET /api/v1/graph/entities?name=X` 能查到 |
| Leiden 全量重建耗时 | ≤ 10 分钟（10 万节点规模） | admin API job 状态 `running` → `done` |
| WebSocket 重连恢复时间 | ≤ 5s（含 SessionState 从 Redis 恢复） | 客户端模拟重连测试 |

### 5.1.1 性能预算分解（端到端 1.5s P50 的来源）

| 阶段 | P50 预算 | 说明 |
|---|---|---|
| 客户端采集 + Opus 编码 | 50ms | 浏览器 WebAudio API |
| 网络上行（24kbps Opus） | 100ms | 门店宽带 4Mbps 上行 |
| 服务端 Opus 解码 | 5ms | libopus |
| Silero VAD 推理（32ms 块） | 15ms | CPU 模式 |
| funASR WebSocket 往返 | 400ms | 含 600ms lookahead 中 60% 重叠摊销 |
| 服务端事件 JSON 序列化 | 10ms | 或 msgpack 二进制 |
| 网络下行 | 100ms | 同上 |
| 前端渲染 | 50ms | React + requestAnimationFrame |
| **小计** | **725ms** | 留 ~775ms buffer 应对抖动 |

预算分解验证：1.5s P50 在常规网络下可达；P95 受 funASR 排队影响可能到 3s，符合 §5.1 目标。

### 5.2 GPU 资源

| 模型 | GPU 内存 | 备注 |
|---|---|---|
| Silero VAD（CPU 模式） | 0 MB | Silero 在 ARM/x86 CPU 上 < 50ms/块 |
| funASR paraformer-zh-streaming | ~1.5GB GPU | 单 GPU（T4/A10）可支撑 ≥ 20 并发流 |
| Voiceprint 提取（M7 ECAPA-TDNN） | ~800MB GPU | 仅在 confirmed 段触发，不在每个 PCM 块触发 |
| GraphRAG 实体抽取（GLM-4-Flash） | 0（API 调用） | 复用 M5 既有 LLMAdapter |

### 5.3 PIPL / 数据安全

- WebSocket 必须走 `wss://`（TLS 终止在 nginx）。
- `SessionState` 中的 PCM 缓冲最长保留 60s，超时强制 flush 或丢弃（PIPL 数据最小化原则）。
- Voiceprint 加密复用 M7 `AudioCrypto.encrypt_bytes()`（speaker_linker.py:469 已实现）。
- WebSocket 鉴权：JWT in query string（`?token=...`），TTL 5 分钟，刷新走 REST `/api/v1/auth/refresh`。
- 审计：所有 `/ws/stream` 连接建立/断开、所有 confirmed 段入库、所有 Leiden 重建，均写 `audit_log`。

### 5.4 可观测性

| 信号 | 实现 |
|---|---|
| Metrics | Prometheus：`streaming_active_sessions`、`streaming_e2e_latency_ms`、`streaming_vad_cpu_ms`、`streaming_asr_p95_latency_ms`、`streaming_confirm_segments_total`、`streaming_delta_skipped_total`（hash 命中） |
| Logs | 结构化 JSON；关键字段：`session_id`、`recording_id`、`tenant_id`、`event`（`chunk_in`/`vad_result`/`asr_delta`/`segment_confirmed`/`tag_batch`） |
| Tracing | OpenTelemetry；span 链：`ws_recv → vad → asr → extractor → merger → db_write`；trace_id 通过 WebSocket 首帧下发，便于前端关联 |
| Healthcheck | `GET /api/v1/health/streaming`：返回 funASR WebSocket 连通性、Silero 模型加载状态、当前活跃会话数 |

### 5.5 SLO 与错误预算

| SLO | 目标 | 错误预算（月度） |
|---|---|---|
| `/ws/stream` 可用性 | 99.5% | 3.6h 不可用 |
| 端到端延迟 P95 ≤ 3s | 99% 的会话达标 | 7.2h 延迟超标 |
| Confirmed 段丢失率 | < 0.5% | 3.6k 段/百万段 |
| funASR WebSocket 重连 | < 1 次/小时 | 7.2k 重连/月 |

错误预算耗尽时触发：
- 冻结新功能上线（仅允许 bugfix）。
- 自动告警主理人 + 架构师。
- Postmortem 流程启动（24h 内复盘）。

### 5.6 容量规划

基于 §5.1 单节点 50 并发 + §5.2 GPU 资源：

| 集群规模 | 同时在线会话 | GPU 占用 | 月度成本（估算） |
|---|---|---|---|
| 1 节点（4 core CPU + 1×T4） | 50 | 2.3GB / 16GB | ¥2k |
| 3 节点（HA） | 150 | 6.9GB / 48GB | ¥6k |
| 10 节点（连锁品牌区域） | 500 | 23GB / 160GB | ¥20k |

成本主要受 GPU 显存驱动；如客户量级 < 50 并发，建议共享 funASR 实例（多租户）。

---

## 6. Dependency Graph

```mermaid
graph TD
    subgraph "M1-M7 (unchanged, L10)"
        CHUNKER[core/chunker.py<br/>batch]
        EXTRACTOR[core/extractor.py<br/>batch entity extraction]
        MERGER[core/entity_merger.py<br/>M6]
        LINKER[core/speaker_linker.py<br/>M7]
        VAD_BATCH[adapters/real/vad_silero.py<br/>HTTP]
        ASR_BATCH[adapters/real/funasr.py<br/>HTTP]
        REST[api/*.py<br/>14 routers]
    end

    subgraph "M8 New (this PRD)"
        SVAD_PROTO[adapters/protocols.py<br/>+ StreamingVADAdapter]
        SASR_PROTO[adapters/protocols.py<br/>+ StreamingASRAdapter]
        SVAD_IMPL[adapters/real/streaming_vad_silero.py<br/>Silero 4-state]
        SASR_IMPL[adapters/real/streaming_funasr.py<br/>WebSocket:10095]
        SESSION[core/streaming/session_state.py<br/>SessionState]
        WS[api/streaming.py<br/>/ws/stream]
        DELTA[core/streaming/delta_detector.py<br/>content-hash]
        BTAG[core/streaming/batch_tagger.py<br/>N=5]
        EDMGES[core/streaming/edge_confidence.py<br/>EXTRACTED/INFERRED/AMBIGUOUS]
        REBUILD[api/graph.py<br/>+ POST /rebuild]
        FUZZY[core/speaker_fuzzy_matcher.py<br/>SpeakerFuzzyMatcher]
    end

    subgraph "External"
        FUNASR_WS[funASR server<br/>WebSocket:10095]
        SILERO_MD[Silero VAD model<br/>silero_vad.onnx]
    end

    WS --> SESSION
    SESSION --> SVAD_IMPL
    SESSION --> SASR_IMPL
    SVAD_IMPL --> SILERO_MD
    SASR_IMPL --> FUNASR_WS
    SESSION -->|confirmed segs| EXTRACTOR
    EXTRACTOR --> MERGER
    EXTRACTOR --> LINKER
    SESSION --> DELTA
    DELTA --> EXTRACTOR
    SESSION --> BTAG
    BTAG -->|tag_facts| TAGS[(tag three-layer model)]
    LINKER --> FUZZY
    REBUILD -->|triggers| COMM[core/community.py<br/>Leiden full]

    SVAD_PROTO -.validated by.-> SVAD_IMPL
    SASR_PROTO -.validated by.-> SASR_IMPL

    classDef locked fill:#fef3c7,stroke:#f59e0b
    class WS,SESSION,SVAD_IMPL,SASR_IMPL locked
```

---

## 7. Risks

### R1: WebSocket 复杂度（连接生命周期 / 鉴权 / 重连 / 背压）

**严重度**：高
**概率**：高
**描述**：FastAPI WebSocket 与 REST 在中间件链（auth、tenant、audit）上的行为不一致；客户端断线重连时 SessionState 恢复逻辑复杂；背压（客户端推 PCM 过快）可能导致服务端 OOM。
**缓解**：
- 鉴权走 query string JWT，在 `Depends(get_current_user)` 复用 REST 链路（已验证 FastAPI 支持 WS Depends）。
- SessionState 持久化到 Redis（TTL 60s），重连时通过 `session_id` 恢复。
- 背压：服务端检测 `recv_queue.qsize() > 100` 时下发 `event=backpressure` 警告，> 200 时强制关闭连接（fail-fast）。
**Owner**：架构师

### R2: 实体漂移（Entity Drift）

**严重度**：高
**概率**：中
**描述**：流式产生的段切分与批处理不同（VAD 边界差异），导致同一段文本在流式下抽出 `["长安 CS75", "退订"]`，批处理下抽出 `["长安 CS75 Plus", "退订意向"]`。增量合并后图谱出现重复实体或矛盾关系。
**缓解**：
- L8 content-hash 在 chunk 层去重，但**不能**消除段切分差异。
- L5 EntityMerger 的 rapidfuzz fuzzy（threshold=0.85）能合并 `CS75` ↔ `CS75 Plus`，但**不能**处理语义近邻（`退订` ↔ `退订意向`）。
- M8 引入 `EdgeConfidence.AMBIGUOUS` 标签，所有流式产生的实体-关系默认带 `streaming=true` 元数据，下游 RAG 检索时可降权。
- 长期方案（M9+）：bi-temporal 图谱 + 离线回滚。
**Owner**：架构师 + 许清楚

### R3: Silero VAD 隐状态漂移

**严重度**：中
**概率**：中
**描述**：Silero VAD 的 LSTM 隐状态必须按顺序喂入每个 512 采样块；任何丢包、重排、跳块会导致后续预测全部失效。WebSocket 网络抖动可能造成块乱序。
**缓解**：
- 协议要求客户端附带 `seq` 单调递增序号；服务端检测 `seq` 跳变时强制 reset 隐状态（牺牲一段的准确度，换稳定性）。
- 备选：每个块携带前一块的 hash，服务端校验连续性。
**Owner**：流式 VAD owner

### R4: funASR WebSocket 稳定性

**严重度**：中
**概率**：中
**描述**：funASR server 在并发 ≥ 20 时偶发崩溃（社区 issue 反馈，无官方修复）。M8 单节点目标 50 并发，超过 funASR 上限。
**缓解**：
- 部署 funASR 集群（≥ 2 副本），客户端做 round-robin 负载均衡。
- 单 funASR 副本限流 20 流，超过时排队（前端显示"等待 ASR 资源"）。
- 监控 `funasr_ws_reconnect_total`，超过阈值告警。
**Owner**：运维

### R5: 增量社区检测质量退化

**严重度**：中
**概率**：高（如不做 L6 全量重建）
**描述**：L6 决策为"不做增量 Leiden"，意味着流式产生的实体-关系边在两次 rebuild 之间**不参与**社区划分。RAG 检索的 community-report 节点会逐渐过时。
**缓解**：
- 默认每日凌晨 02:00 自动触发 `POST /api/v1/graph/rebuild`（cron）。
- 监控 `graph_edges_since_last_rebuild` 指标，超过阈值（如 1000 条新边）自动触发。
- 文档明确告知用户：community-report 的 fresh window ≤ 24h。
**Owner**：架构师

### R6: LLM 调用成本失控

**严重度**：中
**概率**：高
**描述**：每 5 段触发一次抽取 + 打标 LLM 调用；高并发（50 会话 × 每会话每小时 200 段 = 2000 段/h，400 次 LLM 调用/h）可能导致 OpenAI/智谱 API 月账单失控。
**缓解**：
- 复用 M5 `_cached_complete()` 双层缓存（file_index Layer 2 + adapter Layer 1）。
- L8 content-hash 在 chunk 层去重，重复内容不触发抽取。
- 配置 `streaming_llm_hourly_budget`，超过时降级为"仅 VAD+ASR，不抽取"模式（前端显示降级提示）。
**Owner**：PM + 架构师

### R7: 双态文本协议前端实现复杂度

**严重度**：低
**概率**：中
**描述**：前端需要同时维护 realtime（高频更新，每 200ms）与 confirmed（低频更新，每 10s）两条文本流，UI 状态机复杂。
**缓解**：
- Appendix A 提供完整 WebSocket 消息 schema，前端可直接生成 TypeScript 类型。
- 提供 reference implementation（React hook `useStreamingTranscript`）。
**Owner**：前端

### R8: 回归 M1–M7 批处理路径

**严重度**：高
**概率**：低
**描述**：尽管 L10 要求"纯增量"，但 `adapters/protocols.py` 新增 Protocol 可能影响既有 import；`extractor.py` 的增量改造若不慎可能污染批处理路径。
**缓解**：
- CI 增加"批处理回归套件"（M1–M7 全部 eval cases），任何 PR 必须全绿。
- Protocol 新增采用**追加**模式，不修改既有 6 个 Protocol 的字段。
- `extractor.py` 的增量入口采用新方法 `extract_from_streaming_batch()`，**不**改 `extract_from_chunks()`。
**Owner**：CI/CD owner

### R9: 门店网络不稳定

**严重度**：中
**概率**：高（门店宽带普遍 4–8Mbps，且多人共用）
**描述**：16kHz PCM × 16bit × 1ch = 256kbps，单会话占用 12.5% 带宽；多设备同时上传会触发路由器 QoS 限速，导致 WebSocket 块延迟。
**缓解**：
- 客户端启用 Opus 编码（16kHz mono @ 24kbps），服务端解码后再喂 Silero。
- 网络检测：客户端每 5s 测 RTT，> 500ms 时切换为"批降级模式"（本地缓冲 30s 再上传）。
**Owner**：客户端

### R10: 隐私 / 录音同意

**严重度**：高
**概率**：低（但有合规风险）
**描述**：实时流式录音必须事先获得客户同意；部分地区（如欧盟 GDPR）要求双份同意（录音 + 实时分析）。
**缓解**：
- WebSocket 建连时强制携带 `consent_token`（门店端硬件/软件生成的客户同意凭证）。
- 缺失 token 时服务端立即关闭连接（status code 4001）并审计。
- 文档明确：AudioGraphy 不负责 consent 收集，由门店端系统保证。
**Owner**：法务 + PM

---

## 8. Acceptance Criteria

### 8.1 模块测试覆盖率

> **规则（继承自 M6/M7）**：**per-module ≥ 85% OR total ≥ 88%**。
> 即：单模块允许低于 88%，但不得低于 85%；整体覆盖率必须 ≥ 88%。

| 模块 | 路径 | 覆盖率目标 |
|---|---|---|
| `core/streaming/session_state.py` | SessionState 生命周期 | ≥ 90% |
| `core/streaming/delta_detector.py` | content-hash 比对 | ≥ 95%（逻辑简单，无理由低于） |
| `core/streaming/batch_tagger.py` | N=5 批量打标 | ≥ 85% |
| `core/streaming/edge_confidence.py` | EXTRACTED/INFERRED/AMBIGUOUS 标签 | ≥ 90% |
| `adapters/real/streaming_vad_silero.py` | Silero 4-态 | ≥ 80%（集成测试难，单测靠 mock） |
| `adapters/real/streaming_funasr.py` | funASR WebSocket | ≥ 80%（同上） |
| `api/streaming.py` | `/ws/stream` 端点 | ≥ 85% |
| `core/speaker_fuzzy_matcher.py`（P1） | rapidfuzz on display_name | ≥ 90% |
| **整体（M8 新增代码）** | — | **≥ 88%** |

### 8.2 功能验收（P0）

- [ ] **AC-P0-01**: WebSocket `/ws/stream` 可建连，鉴权失败返回 4001。
- [ ] **AC-P0-02**: 客户端推送 60s 模拟音频，服务端产生 ≥ 4 个 confirmed 段。
- [ ] **AC-P0-03**: 每个 confirmed 段触发 EntityMerger + SpeakerLinker，图谱中可查到对应实体。
- [ ] **AC-P0-04**: content-hash 重复的 chunk 不触发重复抽取（`streaming_delta_skipped_total` ≥ 1）。
- [ ] **AC-P0-05**: 边置信度标签写入数据库（`SELECT COUNT(*) FROM edges WHERE confidence_tag='EXTRACTED'` ≥ 1）。
- [ ] **AC-P0-06**: 门店实时质检 demo（US 2.1）端到端跑通，SLA 达标（mic → frontend P50 ≤ 1.5s）。
- [ ] **AC-P0-07**: 批处理回归套件（M1–M7 eval）全绿，证明 L10 向后兼容。

### 8.3 功能验收（P1）

- [ ] **AC-P1-01**: `POST /api/v1/graph/rebuild` 返回 job_id，异步 Leiden 重算完成后 `GET /rebuild/{job_id}` 返回 `done`。
- [ ] **AC-P1-02**: 连续 5 个 confirmed 段后，tag 三层模型有新增（`tag_facts` 新增行）。
- [ ] **AC-P1-03**: SpeakerFuzzyMatcher 在 display_name 模糊匹配测试集上 F1 ≥ 0.80。
- [ ] **AC-P1-04**: 检测到 `投诉意图` 实体时，webhook 投递延迟 ≤ 30s，5 分钟去重生效。

### 8.4 性能验收

- [ ] **AC-PERF-01**: 50 并发会话稳定运行 30 分钟，无 OOM、无 WebSocket 异常断开（> 5%）。
- [ ] **AC-PERF-02**: 单会话内存峰值 ≤ 1.5GB（US 2.3 长录音场景）。
- [ ] **AC-PERF-03**: funASR WebSocket 重连次数 ≤ 1 次/小时（正常网络下）。

### 8.5 安全验收

- [ ] **AC-SEC-01**: `wss://` 强制（HTTP 升级请求被拒绝）。
- [ ] **AC-SEC-02**: 缺失 `consent_token` 的连接被拒绝并审计。
- [ ] **AC-SEC-03**: Voiceprint 加密落库（`voiceprint_vectors.vector_encrypted` 非 NULL）。

---

## 9. Open Questions（待架构师决策）

### Q1: funASR WebSocket 多租户隔离方案

**背景**：funASR server 本身无 tenant 概念，单实例对所有流入文本无差别处理。AudioGraphy 是 tenant-scoped 的（EntityMerger、SpeakerLinker 均按 tenant_id 隔离）。
**问题**：是否在 AudioGraphy 侧（StreamingFunASRAdapter）为每个 tenant 维护独立 WebSocket 连接到 funASR？还是单连接复用、通过 session_id 在协议层区分？
**影响**：连接数 / 资源隔离 / 故障爆炸半径。
**建议**：单连接复用 + session_id 路由（funASR 协议已支持），但需要在 adapter 层增加 tenant 上下文回传校验。

### Q2: Silero VAD 状态机 reset 策略

**背景**：R3 提到 WebSocket 块乱序时需要 reset 隐状态。但 reset 意味着丢失前一段语音的上下文（可能丢失正在进行的 speech 段）。
**问题**：reset 触发条件设为多严格？
  - (a) 任何 seq 跳变即 reset（保守，丢失多）
  - (b) seq 跳变 > 3 块（≈ 100ms）才 reset（折衷）
  - (c) 永不 reset，依赖客户端重传（带宽翻倍）
**影响**：准确性 vs 鲁棒性权衡。
**建议**：(b)，并在 reset 后下发 `event=vad_reset` 给前端，由前端决定是否回放缓冲。

### Q3: EdgeConfidence.AMBIGUOUS 的下游消费策略

**背景**：L9 引入三标签，但 RAG 检索时如何使用尚未明确。
**问题**：检索时是否默认过滤 `AMBIGUOUS` 边？还是降权？降权系数多少？
**影响**：召回率 / 准确率 / 用户感知。
**建议**：默认保留但降权 0.5；提供检索参数 `min_confidence=EXTRACTED` 供严格模式使用。需要架构师确认是否在 M8 暴露该参数，还是 M9 再做。

### Q4: 流式 webhook 去重窗口实现

**背景**：P1-4 要求 5 分钟去重。
**问题**：去重状态存 Redis 还是数据库？过期清理策略？
**建议**：Redis（key: `wh_dedup:{recording_id}:{alert_type}`，TTL 300s）；崩溃恢复时接受少量重复（< 5%）。

### Q5: 长录音降内存模式（P2-1）何时启动

**背景**：US 2.3 长录音场景，需要客户端主动选择"流式但不实时展示"。
**问题**：通过什么信号告知服务端进入"低内存模式"？是否复用 `/ws/stream` 端点（带 query param）还是新增 `/ws/ingest`？
**建议**：M8 先观察，P2 实现时再决策；当前 P0/P1 不阻塞。

---

## 10. Appendices

### Appendix A: funASR WebSocket Streaming Protocol（参考实现规范）

**端点**：`ws://funasr-server:10095`（M8 不直接暴露给前端，由 `StreamingFunASRAdapter` 服务端中转）

**初始化握手**（客户端 → 服务端，JSON）：

```json
{
  "mode": "2pass",
  "chunk_size": [5, 10, 5],
  "chunk_interval": 10,
  "wav_name": "session_<session_id>",
  "wav_format": "pcm",
  "is_speaking": true,
  "hotwords": "{\"长安CS75\":1,\"退订意向\":1}",
  "itn": true,
  "audio_fs": 16000
}
```

字段说明：
- `mode`: `"2pass"` = 流式 + 一次性修正（推荐）；`"online"` = 纯流式（低延迟，CER 略高）；`"offline"` = 等价批处理。
- `chunk_size`: `[5,10,5]` 表示 600ms lookahead，24 帧累积（L2 锁定）。
- `chunk_interval`: 每累积 10 个 chunk 输出一次 delta。
- `hotwords`: M8 复用 M5 `entity_aliases` 表的 canonical_text 作为热词（每会话拉取一次）。
- `itn`: Inverse Text Normalization（数字/日期标准化）。

**音频推送**（客户端 → 服务端，二进制）：

- 格式：little-endian float32 PCM。
- 单块：8192 bytes（= 2048 采样 = 128ms @ 16kHz）。
- 不带任何 header；服务端按接收顺序喂入 paraformer。

**结束信号**（客户端 → 服务端，JSON）：

```json
{"is_speaking": false}
```

**Delta 响应**（服务端 → 客户端，JSON）：

```json
{
  "mode": "2pass-online",
  "text": "我想退订",
  "wav_name": "session_abc123",
  "is_final": false
}
```

- `mode`: `"2pass-online"` = 中间 delta（realtime）；`"2pass-offline"` = 句终修正（confirmed）。
- 收到 `"is_final": true` 即一段 confirmed 结束，触发 `SessionState.on_confirm()`。

**M8 适配器行为**：

```python
class StreamingFunASRAdapter:
    async def connect(self) -> None: ...
    async def push_pcm(self, pcm: bytes, seq: int) -> ASRDeltaResult:
        # Send binary; await next JSON delta; map to ASRDeltaResult.
        ...
    async def finalize(self) -> ASRResult:
        # Send {"is_speaking": false}; drain pending deltas until is_final.
        ...
    async def aclose(self) -> None: ...
```

**异常映射**（继承批处理 funasr.py:240 模式）：

| funASR 错误码 | 异常 | 处理 |
|---|---|---|
| 400 (bad handshake) | `StreamingASRRequestError` | 关闭 WebSocket，前端提示重连 |
| 1011 (internal error) | `StreamingASRServerError` | 重试 3 次后降级到批处理 |
| 连接超时（5s） | `StreamingASRConnectTimeout` | 切换到备用 funASR 副本 |

### Appendix B: Silero VAD Streaming State Machine

**采样配置**：
- Sample rate: 16000 Hz
- Chunk size: 512 samples（32ms）
- Overlap: 0（Silero 隐状态跨块保留，不需要显式 overlap）

**4-态状态机**：

```
                    onset >= 0.5
        ┌──────────────────────────────────┐
        ▼                                  │
   ┌─────────┐    onset >= 0.5     ┌──────────────┐
   │ SILENCE │ ──────────────────► │ PENDINGSPCH  │
   └─────────┘                     └──────────────┘
        ▲                                  │
        │                                  │ pending_duration
        │                                  │   >= min_speech (0.25s)
        │                                  ▼
        │                           ┌─────────┐
        │     offset < 0.35         │ SPEECH  │
        │ ┌────────────────────────►│         │
        │ │                         └─────────┘
        │ │                               │
        │ │                               │ offset < 0.35
        │ │                               ▼
        │ │                        ┌──────────────┐
        │ │     pending_duration   │ PENDINGSIL   │
        │ └──────── < min_silence ─│              │
        │       (0.10s)            └──────────────┘
        │                               │
        │                               │ pending_duration
        │                               │   >= min_silence (0.10s)
        └───────────────────────────────┘
```

**状态转移规则**：

| 当前态 | 输入 | 下一态 | 副作用 |
|---|---|---|---|
| `SILENCE` | onset ≥ 0.5 | `PENDING_SPEECH` | 记录 `pending_start = current_ts` |
| `SILENCE` | onset < 0.5 | `SILENCE` | — |
| `PENDING_SPEECH` | 时间累积 ≥ 0.25s | `SPEECH` | emit segment_start at `pending_start` |
| `PENDING_SPEECH` | onset < 0.5 | `SILENCE` | 清除 `pending_start` |
| `SPEECH` | onset ≥ 0.5 | `SPEECH` | — |
| `SPEECH` | onset < 0.35 | `PENDING_SILENCE` | 记录 `pending_silence_start` |
| `PENDING_SILENCE` | 时间累积 ≥ 0.10s | `SILENCE` | emit segment_end at `pending_silence_start` |
| `PENDING_SILENCE` | onset ≥ 0.5 | `SPEECH` | 清除 `pending_silence_start`（语音继续） |

**隐状态管理**：

```python
@dataclass(slots=True)
class SileroHiddenState:
    """LSTM hidden state — MUST be passed chunk-to-chunk."""
    h: torch.Tensor  # shape (2, 1, 64)
    c: torch.Tensor  # shape (2, 1, 64)

class StreamingSileroVADAdapter:
    def __init__(self):
        self._state = SileroHiddenState(...)  # init zeros
        self._fsm = VADFSM(...)

    async def push_chunk(self, pcm: bytes) -> VADChunkResult:
        samples = np.frombuffer(pcm, dtype=np.int16)
        onset_score = self._model(samples, self._state).item()  # updates _state in-place
        transition = self._fsm.step(onset_score)
        return VADChunkResult(onset=onset_score, transition=transition, ...)

    def reset_state(self) -> None:
        """On seq gap / session resume / explicit reset."""
        self._state = SileroHiddenState(...)
        self._fsm = VADFSM(...)
```

**阈值默认值**（L3 锁定，可通过 settings 覆盖）：

```python
# config.py additions
streaming_vad_onset_threshold: float = 0.5
streaming_vad_offset_threshold: float = 0.35
streaming_vad_min_speech_sec: float = 0.25
streaming_vad_min_silence_sec: float = 0.10
streaming_vad_chunk_samples: int = 512  # do not change; Silero contract
```

### Appendix C: 数据库 schema 变更详情

#### C.1 `edges` 表新增列

```sql
ALTER TABLE edges
    ADD COLUMN confidence_tag VARCHAR(16) NOT NULL DEFAULT 'EXTRACTED'
        CHECK (confidence_tag IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
    ADD COLUMN streaming_origin BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN source_session_id VARCHAR(64) NULL;  -- 仅流式产生时填，便于回溯
```

- `confidence_tag`（L9）：默认 `EXTRACTED`，批处理路径不修改该列（保持默认）。
- `streaming_origin`：流式产生为 TRUE，批处理产生为 FALSE；下游查询可按需过滤。
- `source_session_id`：可追溯到具体 WebSocket 会话，便于 R2（实体漂移）调查。

索引：
```sql
CREATE INDEX idx_edges_streaming ON edges(streaming_origin, tenant_id) WHERE streaming_origin = TRUE;
CREATE INDEX idx_edges_confidence ON edges(tenant_id, confidence_tag);
```

#### C.2 `streaming_sessions` 新表

```sql
CREATE TABLE streaming_sessions (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL UNIQUE,  -- 客户端生成，用于重连
    tenant_id VARCHAR(64) NOT NULL,
    recording_id BIGINT NOT NULL REFERENCES recordings(id),
    user_id BIGINT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ NULL,
    last_chunk_at TIMESTAMPTZ NULL,
    seg_confirmed_count INT NOT NULL DEFAULT 0,
    seg_realtime_count INT NOT NULL DEFAULT 0,
    bytes_in BIGINT NOT NULL DEFAULT 0,
    error_count INT NOT NULL DEFAULT 0,
    end_reason VARCHAR(32) NULL,  -- 'normal' | 'client_disconnect' | 'server_shutdown' | 'error' | 'backpressure'
    consent_token_hash VARCHAR(64) NOT NULL  -- sha256(consent_token)
);
CREATE INDEX idx_streaming_sessions_tenant_started ON streaming_sessions(tenant_id, started_at DESC);
CREATE INDEX idx_streaming_sessions_recording ON streaming_sessions(recording_id);
```

#### C.3 `EntityAlias` 表（M6 既有）

无需 schema 变更。M8 的 `StreamingFunASRAdapter` 在建连时从 `entity_aliases` 拉取 canonical_text 作为热词注入 funASR。

#### C.4 Alembic 迁移脚本

```
migrations/versions/2026_07_22_m8_streaming_init.py
```

- Upgrade：执行上述 SQL + 创建 `streaming_sessions` 表。
- Downgrade：DROP COLUMN + DROP TABLE；**注意**：已写入的 `edges.confidence_tag` 值会丢失，但批处理路径默认值 `EXTRACTED` 不受影响。

回滚策略：
- 生产环境先在 staging 跑完整升级 + 降级，验证零数据丢失。
- 降级脚本要求在非高峰时段执行，预计 < 5 分钟（10 万 edges 规模）。

### Appendix D: WebSocket 事件协议（完整 schema）

#### D.1 客户端 → 服务端

**事件 1：连接初始化（首帧，JSON）**

```json
{
  "type": "init",
  "session_id": "uuid-v4",
  "recording_id": 12345,
  "consent_token": "base64-...",
  "client_info": {
    "user_agent": "...",
    "opus_supported": true,
    "network_type": "wifi"
  }
}
```

**事件 2：音频块（二进制）**

```
[4 bytes: seq (uint32 big-endian)] [N bytes: Opus or PCM payload]
```

- `seq`：从 0 开始单调递增；服务端检测跳变触发 R3 缓解策略。
- Opus 模式：每块 40ms 音频，~120 bytes。
- PCM 模式（Opus 不可用时的 fallback）：每块 32ms @ 16kHz = 1024 bytes。

**事件 3：结束（JSON）**

```json
{"type": "finalize"}
```

#### D.2 服务端 → 客户端

**事件 A：`session_opened`**

```json
{
  "type": "session_opened",
  "session_id": "uuid-v4",
  "trace_id": "otel-trace-id",
  "server_time": "2026-07-22T10:00:00Z",
  "capabilities": {
    "max_buffer_chunks": 200,
    "vad_reset_strategy": "seq_gap_3"
  }
}
```

**事件 B：`realtime_text`（高频）**

```json
{
  "type": "realtime_text",
  "session_id": "uuid-v4",
  "seq": 42,
  "text": "我想退订",
  "is_final": false,
  "timestamp_ms": 1708910000123
}
```

**事件 C：`segment_confirmed`（低频，进入图谱）**

```json
{
  "type": "segment_confirmed",
  "session_id": "uuid-v4",
  "segment_id": 678,
  "start_sec": 12.34,
  "end_sec": 18.56,
  "transcript": "我想退订现在的套餐。",
  "speaker_id": "spk_0",
  "entities": [
    {"name": "退订意向", "type": "Action", "confidence_tag": "EXTRACTED"}
  ]
}
```

**事件 D：`tags_updated`（N=5 触发）**

```json
{
  "type": "tags_updated",
  "session_id": "uuid-v4",
  "recording_id": 12345,
  "tag_batches": [
    {
      "segment_ids": [678, 679, 680, 681, 682],
      "tags": ["话术偏离", "投诉倾向"],
      "severity": "warning"
    }
  ]
}
```

**事件 E：`error` / `backpressure` / `vad_reset`**

```json
{
  "type": "backpressure",
  "session_id": "uuid-v4",
  "queue_depth": 150,
  "message": "Queue depth exceeded threshold; please slow down"
}
```

```json
{
  "type": "error",
  "code": "FUNASR_UNAVAILABLE",
  "message": "ASR server unreachable; please reconnect",
  "recoverable": true
}
```

**事件 F：`session_closed`**

```json
{
  "type": "session_closed",
  "session_id": "uuid-v4",
  "reason": "normal",
  "stats": {
    "duration_sec": 300,
    "seg_confirmed_count": 24,
    "bytes_in": 1500000
  }
}
```

### Appendix E: 本 PRD 自检

| 检查项 | 目标 | 实际 |
|---|---|---|
| 总行数 | 800–1200 | 见文件末尾行号 |
| 章节数 | 12（TL;DR + Background + User Stories + Scope + Locked Decisions + Non-Functional + Dep Graph + Risks + Acceptance + Open Questions + Appendices A & B + 自检） | 12 ✅ |
| Locked Decisions | L1–L10 全部 verbatim | ✅ §4 表格 |
| 风险数 | ≥ 5（含 WebSocket 复杂度 + 实体漂移） | 10（R1 + R2 满足专项要求） |
| 验收规则 | per-module ≥ 85% OR total ≥ 88% | §8.1 ✅ |
| 门店实时质检场景 | 必含 | §2.1 ✅ |
| Appendix A | funASR WebSocket 协议 | ✅ |
| Appendix B | Silero VAD 状态机 | ✅ |
| 与锁定决策偏离 | 理论为 0 | **0**（所有描述均围绕 L1–L10 展开，未引入新决策） |

### Appendix F: 与 m7-prd.md 的结构对照

| 章节 | m7-prd.md | 本 PRD | 一致性 |
|---|---|---|---|
| TL;DR | ✅ | ✅ §0 | ✅ |
| Background | ✅ | ✅ §1 | ✅ |
| User Stories + 验收矩阵 | ✅ | ✅ §2.4 | ✅ |
| P0/P1/P2 | ✅ | ✅ §3 | ✅ |
| Locked Decisions L1–L10 | ✅ | ✅ §4 | ✅ |
| Non-Functional（perf/GPU/PIPL/observability） | ✅ | ✅ §5 | ✅ |
| Mermaid 依赖图 | ✅ | ✅ §6 | ✅ |
| Risks | ✅ | ✅ §7（10 个） | ✅ |
| Acceptance Criteria | ✅（per-module ≥ 85% OR total ≥ 88%） | ✅ §8（同规则） | ✅ |
| Open Questions | ✅ Q1–Q5 | ✅ §9 Q1–Q5 | ✅ |
| Appendices A/B | ✅ | ✅ §10 A/B + C/D | ✅ |

---

**PRD 终点。** 任何修改 L1–L10 的请求需主理人齐活林 + 架构师双签；其余章节（P1/P2 范围、风险缓解、Open Questions 建议）可在 review 中迭代。
