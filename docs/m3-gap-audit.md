# AudioGraphy 深度差异审计报告 · DESIGN.md vs 当前实现

> **审计日期**: 2026-07-20
> **审计范围**: `docs/DESIGN.md`（1109 行 17 章）vs `backend/audio_graphy/`（38 .py）+ 前端骨架
> **审计模式**: 只读审计，未修改任何文件
> **目的**: 为后续"完整详细闭环实现"提供缺口清单与工作项排序

---

## 总览结论（一句话）

**当前实现处于"算法内核已就位、应用层全缺位"的状态**：M1+M2 完成了 §3 核心算法（chunker / extractor / graph / retrieval / rerank）和 §7 存储层（file_index / mysql_vector / graph_networkx）以及 §6 标签三层数据模型（13 张 ORM 表已建好），但是 §2 三服务层（Ingestion/Query/Governance）、§12 REST API、§14 鉴权多租户、§8 评估方案、§13 前端 UI、§10 中 `api/ auth/ tags/ eval/ scheduler/ services/` 子目录——**全部 0 行实现**。`main.py` 只有 `/health` 一个端点，前端只有一个写死"frontend stub"的 App.tsx。

**对"端到端闭环"的影响**：用户目前完全无法 `upload → index → query → tag → recompute → view`——任何一步都缺入口。

---

## Part A · 严重缺口（P0 — 闭环必需）

### A1. REST API 层完全缺失（阻塞全部闭环）
- **章节引用**：§12.1 第 646-671 行
- **设计要求**：18 条 REST 端点，覆盖 recordings / query / graph / tags / prompts / eval / admin 7 个资源组，全部 `/api/v1` 前缀
- **当前状态**：`backend/audio_graphy/main.py:59-75` 仅有 `/health` 和 `/`；**`api/` 目录不存在**
- **缺口影响**：阻塞 ingestion / query / governance 全部三个闭环
- **建议优先级**：**P0**（M3 必做）

### A2. Ingestion Pipeline 服务完全缺失（阻塞 ingestion 闭环）
- **章节引用**：§2.3 第 106 行、§12.3 第 717-729 行
- **设计要求**：录音注册 → VAD → ASR → chunker → bge-m3 → extractor → graph.merge → tag_extractor → tag_facts.append → tag_current.refresh → tag_stats.delta → recording.status=indexed。状态机由 Pipeline worker 拉队列驱动
- **当前状态**：`core/chunker.py` 等 5 个核心算法文件已实现并测试，但**没有任何编排层把它们串起来**——`services/` 目录不存在，`scheduler/` 目录不存在。config 里有 `pipeline_poll_seconds` / `pipeline_concurrency`（`config.py:77-78`）但无消费代码
- **缺口影响**：阻塞 ingestion 闭环——录音无法被注册、无法触发处理、状态机无法推进
- **建议优先级**：**P0**

### A3. Query HTTP 入口缺失（阻塞 query 闭环）
- **章节引用**：§12.3 第 731-742 行、§12.1 第 656 行
- **设计要求**：`POST /query` → weak LLM(rewrite+keywords) → vectors_chunk.cosine_topk + graph.neighbors → filter(recorded_at) → union+dedup+sort → strong LLM(filter as-judge) → rerank → strong LLM(answer) → return {answer, citations[entity→chunk→segment]}
- **当前状态**：`DualChannelRetriever`（`retrieval.py:94`）和 `Reranker`（`rerank.py:88`）已经完整实现四阶段流水线（含 3 级溯源），但是**没有 HTTP 端点暴露**
- **缺口影响**：阻塞 query 闭环——业务方无法触发问答
- **建议优先级**：**P0**

### A4. 标签治理服务缺失（阻塞 governance 闭环）
- **章节引用**：§6.3 第 304-311 行、§6.4 第 313-328 行、§12.3 第 744-754 行
- **设计要求**：tag_facts append-only 写入 / tag_current 视图刷新 / tag_stats 增量聚合 / prompt 版本切换 diff 驱动增量重算 / LLM cache 幂等重打
- **当前状态**：13 张表已建好；**`tags/` 目录不存在**；没有 tag 抽取器
- **缺口影响**：阻塞 governance 闭环
- **建议优先级**：**P0**

### A5. 鉴权与多租户中间件完全缺失
- **章节引用**：§14.1-14.3 第 894-918 行
- **设计要求**：JWT 中间件 / 4 角色 RBAC 矩阵 / 行级隔离 / GraphML 分目录 / PIPL 合规
- **当前状态**：**`auth/` 目录完全不存在**；`config.py:71-74` 有 jwt_* 配置，pyproject 有依赖，但**没有任何签发/校验代码**；ORM 层有 `tenant_id` 列但**没有中间件强制注入**
- **缺口影响**：阻塞 auth 闭环——系统完全开放
- **建议优先级**：**P0**

### A6. 前端 UI 完全缺失
- **章节引用**：§13 全章、§10 第 585-610 行
- **设计要求**：React + Vite + Arco + G6 v5；8 个路由页面；6 个核心组件
- **当前状态**：`frontend/src/` 只有 4 个骨架文件；无业务页面
- **缺口影响**：阻塞全部用户可见闭环
- **建议优先级**：**P0**（图谱浏览器是核心卖点）

### A7. 评估子系统完全缺失
- **章节引用**：§8 全章
- **设计要求**：4 层评估框架 + 开源测试集 + OSS 工具 + 5 维 rubric
- **当前状态**：**`eval/` 目录完全不存在**
- **建议优先级**：**P1**（M4 可启动）

### A8. 真实模型 Adapter 缺失
- **章节引用**：§4.1 第 191-202 行、§15.1 第 924-938 行
- **当前状态**：4 个 mock adapter 已实现；`build_adapters` 在 real 分支 `raise NotImplementedError`；docker-compose 无模型服务
- **建议优先级**：**P1**（M3 在 mock 下验证闭环即可）

---

## Part B · 实现偏差（P1 — 设计不一致）

| ID | 章节 | 偏差描述 | 建议 |
|----|------|---------|------|
| B1 | §12.2 | tag_stats.tag_count（避开保留字）+ 缺独立 updated_at | 接受重命名；updated_at 用 Base 的 |
| B2 | §6.2 | tag_current 是普通表而非物化视图 | M3 实现应用层 upsert；P2 评估 SQL VIEW |
| B3 | §3.3 | retrieval graph channel 把邻居 source_ids 也反查 chunk | 保留实现，加 precision 评估项 |
| B4 | §3.1 | extractor gleaning 有 early termination | 评估 early vs forced 的 P/R 权衡 |
| B5 | §11 | chunker token 估算用 `len//2` 而非 tiktoken | **M3 修复**（tiktoken 已在依赖里） |
| B6 | §15.4 | /health 只返回进程存活，无 readiness | M3 加 /ready 端点 |
| B7 | §3.3 | rerank 的"高精度重转写"未实现 | M4 真实 ASR 时一并补 |
| B8 | §4.1 | ASR Adapter 缺段级时间戳参数 | **M3 修复**（修改 Protocol 签名） |
| B9 | §5.2 | 中文实体归一仅硬编码别名表 | M3 加 rapidfuzz 编辑距离聚类 |

---

## Part C · 设计模糊点（需主理人决策）

| # | 议题 | 候选方案 |
|---|------|---------|
| C1 | 流式扩展（§9）是否预留接口 | A. Phase 1-3 不做，文档加注 / B. M3 预留 streaming/ 骨架 |
| C2 | CLAP/CAM++ 何时上 | A. M3 只跑 Level 1 / B. M3 定义 Protocol 不实现 |
| C3 | tag_current 物化表 vs SQL VIEW | A. 保留表 / B. 换 SQL VIEW |
| C4 | 测试集 license | A. external/ 用 submodule / B. CI 环境拉取 |
| C5 | LLM cache 并发安全 | A. 强制 concurrency=1 / B. 加 asyncio.Lock / C. 移到 MySQL |
| C6 | prompt 版本切换原子性 | A. 全成功才 commit / B. 每条独立 commit / C. 引入 job 表 |

---

## Part D · 完整闭环所需工作清单

> 闭环定义：用户能 **upload → index → query → tag → recompute → view**
> 复杂度：S = 1-2 天，M = 3-5 天，L = 1-2 周

| ID | 描述 | 涉及文件 | 依赖 | 复杂度 |
|----|------|---------|------|--------|
| **W1** | API 骨架 + 中间件链 | 新建 `api/`、改 `main.py` | 无 | M |
| **W2** | Auth 子系统（JWT+RBAC+audit） | 新建 `auth/`、`api/auth.py` | W1 | L |
| **W3** | Recordings ingestion API + 服务 | 新建 `api/recordings.py`、`services/ingestion.py`、`scheduler/` | W1、W2 | L |
| **W4** | Indexing 编排 | 新建 `services/indexing.py` | W3 | M |
| **W5** | Query API + 服务 | 新建 `api/query.py`、`services/query.py` | W1、W4 | M |
| **W6** | Tag 抽取器 | 新建 `core/tag_extractor.py` | W4 | M |
| **W7** | Tag 三层服务 + recompute | 新建 `tags/` | W6 | L |
| **W8** | Tag/Prompt API | 新建 `api/tags.py`、`api/prompts.py` | W7 | M |
| **W9** | Graph 浏览 API | 新建 `api/graph.py` | W4 | M |
| **W10** | Admin API | 新建 `api/admin.py` | W2 | S |
| **W11** | chunker token 估算升级（tiktoken） | 改 `core/chunker.py` | 无 | S |
| **W12** | ASR Protocol 段级签名修复 | 改 `adapters/protocols.py`、`core/chunker.py` | 无 | S |
| **W13** | 健康检查 readiness | 改 `main.py` | W1 | S |
| **W14** | 前端骨架铺开 | 改 `frontend/` | W3 | L |
| **W15** | 图谱浏览器（G6） | 改 `frontend/` | W9、W14 | L |
| **W16** | tag_stats 看板 | 改 `frontend/` | W8、W14 | M |
| **W17** | Prompt 版本管理 UI | 改 `frontend/` | W8、W14 | M |
| **W18** | 评估子系统骨架 | 新建 `eval/` | W4 | L |
| **W19** | 真实 Adapter 实现 | 新建 4 个 adapter、改 `config.py` | 无 | L |
| **W20** | docker-compose 模型服务 | 改 `docker-compose.yml` | W19 | M |
| **W21** | PIPL 合规 | 改 `services/`、新建 `services/retention.py` | W2、W3 | M |
| **W22** | 中文实体归一升级 | 改 `core/extractor.py` | W18 | M |

**总复杂度**：≈ **103 人天**（约 5 人月）

**M3 闭环最小集建议**（P0 优先级，mock 下跑通 upload→index→query→tag→view）：
W1 + W3 + W4 + W5 + W6 + W7 + W8 + W9 + W11 + W12 + W13 + W14 + W15
≈ **44 人天**（约 2 人月，2 后端 + 1 前端并行 4-5 周）

---

## 附：与 §16 路线图进度对照

| 阶段 | 设计目标 | 当前进度 |
|------|---------|---------|
| Phase 1 | 文本图谱 RAG 跑通 | **算法内核 100%、应用层 0%、真实 adapter 0%** |
| Phase 2 | 音频嵌入 + 说话人 | **0%**（flag 有、Protocol 无） |
| Phase 3 | 生产化治理 | **数据模型 100%、服务层 0%、UI 0%** |
| Phase 4 | 流式 | **0%** |

---

**报告结束** · 本报告作为 M3 实施规划的权威缺口清单。
