# AudioGraphy

> **门店录音图谱检索与多级打标系统**
> 
> Store Recording Graph Retrieval & Multi-level Tagging System

把门店录音（汽车销售 / 育儿咨询等）做成知识图谱，用于离线质检、复盘与多级打标。

基于 [VideoRAG](https://github.com/HKUDS/VideoRAG)（KDD 2026, HKUDS）的图谱内核做音频化改造——保留内核，替换模态预处理（VAD + ASR），砍掉视觉附属（MiniCPM-V + ImageBind）。

## M6 Status (2026-07-21)

PIPL §14.3 compliance + Eval REST + rapidfuzz entity clustering shipped.

- **PIPL end-to-end**: AES envelope encryption for audio at rest (master + per-file data key),
  6-category PII scrubbing (phone / id_card / bank_card / email / ipv4 / landline), daily
  03:00 retention sweep with hard-delete, 3 admin-only DSAR endpoints (`/api/v1/dsar/*`),
  fire-and-forget `audit_logs` writer.
- **Eval REST API**: 4 async endpoints under `/api/v1/eval/runs*` (POST create → 202 → poll →
  report download); backed by `eval_runs` table + APScheduler polling worker.
- **`RAGPipeline` + position de-bias**: real `QueryService`-backed pipeline replaces the M5
  stub; each LLM-judge metric runs twice (original + reversed context) and is averaged.
- **rapidfuzz Chinese entity clustering**: `core/entity_merger.py` — 3-layer flow
  (DB alias → rapidfuzz WRatio ≥ 0.85 → new canonical). Tenant-scoped `entity_aliases`
  table; both `entity_f1_strict` and `entity_f1_fuzzy` reported in every eval run.
- **Parenting prompt v1.1**: new `entity_zh_parenting.md` (parenting_consulting scenario)
  registered alongside v1.0 automotive_sales — A/B-able via `POST /prompts/{id}/activate`.
- **Prometheus `/metrics`**:.Counter / Histogram set exposed on port 8000;
  `audiography_http_requests_total`, `audiography_audit_log_written_total`, etc.
- **Root `.env.example`**: mirrors all `config.py` fields with M6 additions
  (`MASTER_KEY_PATH`, `ENTITY_FUZZY_THRESHOLD`, `EVAL_POSITION_DEBIAS`).
- New deps: `rapidfuzz>=3.0`, `prometheus_client>=0.20`, `cryptography>=42.0`.

See [`docs/m6-pipl.md`](./docs/m6-pipl.md) for PIPL compliance guide and
[`docs/m6-eval.md`](./docs/m6-eval.md) for eval REST API reference.

## M5 Status (2026-07-21)

Real **funASR** adapter shipped (OpenAI-compatible `/v1/audio/transcriptions`).
`ADAPTER_ASR_MODE=real` is now supported; image pinned to `funasr/server:1.0.5`
(host port `10095` → container `8000`).

Evaluation subsystem shipped — `audio_graphy.eval`:
- 8 metrics (5 RAG-standard + 3 AudioGraphy-specific), all pure functions.
- LLM-as-judge reuses `LLMOpenAIAdapter(strong)` — no new model service.
- CLI: `python -m audio_graphy.eval --gold-set examples/eval/smoke.yaml`
- Reports: Markdown + JSON, with explicit `MockPipeline` banner.
- Zero new pip dependencies.

See [`docs/m5-eval.md`](./docs/m5-eval.md) for the eval user guide.

## M4 Status (2026-07-21)

Real adapter scaffolding is **code-ready** (no live GPU runs yet).
3 real adapters implemented: VAD (Silero) / LLM (vLLM OpenAI-compatible, strong+weak) /
Embedding (bge-m3). ASR stays mock — funASR lands in M5. To enable:

1. Edit `.env`: set `ADAPTER_{VAD,LLM,EMBED}_MODE=real` + override `JWT_SECRET`.
2. `docker compose --profile real up -d`
3. Verify: `curl http://localhost:8000/health`

See [`docs/deployment.md`](./docs/deployment.md) for hardware requirements and FAQ.

## 📐 设计文档

| 文件 | 说明 |
|---|---|
| [docs/DESIGN.md](./docs/DESIGN.md) | **主设计文档（中英双语）**：17 章 + 2 附录，覆盖架构 / 算法 / 音频适配 / 标签版本化 / 存储 / 评估 / UI / 鉴权 / 部署 / 路线图 |
| [docs/preview.html](./docs/preview.html) | **HTML 预览页**（浏览器双击打开）：Arco Design Web UI 原型 + AntV G6 知识图谱 mock + 嵌入式架构图 |
| [docs/assets/](./docs/assets/) | 6 张架构图 SVG：总体架构 / 索引数据流 / 查询数据流 / 标签版本化 / 存储分层 / 评估数据集选型 |

> ⚠ `preview.html` 通过 CDN 引入 Arco / React / AntV G6，需联网首次打开。

## 🎯 核心定位

- **不是重写**，是 VideoRAG 图谱内核的音频化改造
- **复用优先**：图谱内核 / 存储抽象 / 检索-重排逻辑 0 改动
- **两层分离**：MySQL 管流水线状态/版本/审计，VideoRAG 文件索引管 RAG 检索
- **治理前置**：标签版本化 + 增量重算 + LLM 缓存幂等重打从设计期就内建
- **借鉴开源**：工程产物形态（三件套 + 边置信度）借鉴 [Graphify](https://github.com/Graphify-Labs/graphify)；检索范式借鉴 [Microsoft GraphRAG](https://github.com/microsoft/graphrag)

## 🛠 技术栈

- **后端**：Python 3.13 + FastAPI + SQLAlchemy + MySQL 8
- **前端**：React 18 + Vite + [@arco-design/web-react](https://arco.design) + [@antv/g6](https://g6.antv.antgroup.com/) v5
- **图谱内核**：继承 VideoRAG（LightRAG/nano-graphrag + NetworkX + bge-m3）
- **模型服务**：Silero VAD · funASR（中文）· Qwen3.6-27B/35B vLLM（OpenAI 兼容）· bge-m3
- **部署**：单机 docker-compose

## 🗺 路线图（4 阶段）

| 阶段 | 目标 | 验收 |
|---|---|---|
| Phase 1 | 文本图谱 RAG 跑通（Level 1） | 端到端问答可答，图谱质量达标 — **go/no-go 关卡** |
| Phase 2 | 音频嵌入 + 说话人（Level 2/3） | 声纹驱动跨音频检索可用 |
| Phase 3 | 生产化治理 + Arco UI | 多级打标可重算可审计，前端可用 |
| Phase 4 | 流式扩展（可选） | 边录边查延迟达标 |

详见 [docs/DESIGN.md §16](./docs/DESIGN.md#16-实施路线图--roadmap)。

## 📚 参考方案

- `AudioRAG开发方案.docx`（项目根目录上层）—— 算法/架构原始方案，本工程设计文档是其落地
- [VideoRAG](https://github.com/HKUDS/VideoRAG)（KDD 2026, HKUDS）—— 图谱内核来源
- [Graphify](https://github.com/Graphify-Labs/graphify)（YC S26）—— 工程产物形态参考
- [Microsoft GraphRAG](https://github.com/microsoft/graphrag) —— 检索范式参考

---

**版本**: v1.0 · 2026-07
