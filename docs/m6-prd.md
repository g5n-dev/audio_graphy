# AudioGraphy M6 PRD — PIPL §14.3 + Eval REST + rapidfuzz（Code-Ready）

| 字段 | 值 |
|------|-----|
| 版本 | v6.0.0-draft |
| 作者 | 许清楚（PM / AI 代行） |
| 主理人 | 齐活林 |
| 日期 | 2026-07-21 |
| 前置 | M5 commit `08b02ca`（751 测试 / 92.19% 覆盖率）+ post-M5 audit `cc9b7b2` |
| 范围 | Code-Ready（写代码 + 测试 + docker-compose，不拉起真实服务） |
| 工作流 | WS-1 PIPL §14.3 端到端（W21）／ WS-2 Eval 完整化（W18 收尾）／ WS-3 rapidfuzz 实体聚类（W22）+ 3 Quick Wins |
| Gap Audit 对照 | 关闭 W21 / W22 / W18 + §15.4 Prometheus + §14.3 audit_logs + §15.3 .env.example 完整化；4 大缺口区域中除 UI（M7）外的 3 个全部覆盖 |

---

## 1. 产品目标 + 范围边界

### 1.1 北极星

**让 AudioGraphy 站得住、跑得久、测得准**：

1. **站得住** — PIPL §14.3 端到端：保留期 enforcement + AES-256-GCM 静态加密 + PII 脱敏 + DSAR（GDPR-style access/erasure）。开源用户部署即可满足中国《个人信息保护法》最低线。
2. **跑得久** — Prometheus metrics + audit_logs 全写入 + .env.example 镜像，运维体感从"无可见性"升到"有指标 + 有审计 + 有 schema 同步"。
3. **测得准** — Eval REST API 完整化 + RAGPipeline 实装 + Position de-bias（每个 query 跑 2 次取平均），让 prompt 改动有量化基线。

### 1.2 量化目标

- ✅ `recording_retention_days` 过期后 24h 内自动硬删除音频文件（保留 transcript + tags）
- ✅ 所有音频文件 AES-256-GCM 加密落盘（master key + per-file data key + nonce）
- ✅ PII 脱敏层覆盖 6 类：手机号 / 座机 / 身份证号 / 银行卡号 / 邮箱 / IP
- ✅ `audit_logs` 在 reindex / recompute / prompt-activate / decrypt / delete / export 6 类敏感操作全写入
- ✅ DSAR 端点：`POST /api/v1/dsar/access` + `POST /api/v1/dsar/erasure`（admin only）
- ✅ `POST /api/v1/eval/runs` 启动异步评估；`GET /api/v1/eval/runs/{id}` 查状态；`GET /api/v1/eval/runs/{id}/report` 下载 Markdown
- ✅ `RAGPipeline(EvalPipeline)` 实装：调真实 `services/query.py` 走双通道检索 + LLM 生成
- ✅ Position de-bias：每个 query 跑 2 次（原序 + 反序）取平均
- ✅ `core/extractor.py` 引入 rapidfuzz `fuzz.WRatio`，threshold=0.85 默认（可配置）
- ✅ `entity_aliases` 表（tenant-scoped，DB-backed）+ `prompts/entity_zh_parenting.md` + `versions.yaml` v1.1
- ✅ `/metrics` endpoint（prometheus_client，按服务划分）
- ✅ `.env.example` 镜像 backend pyproject.toml + config.py 全字段
- ✅ M5 全部 751 测试 0 回归，总数 ≥ **840**

### 1.3 In Scope

**WS-1 PIPL §14.3 端到端**
- `core/retention.py`：APScheduler 每日扫描 task → 硬删除过期录音文件
- `core/crypto.py`：AES-256-GCM envelope 加密（cryptography.Fernet，master key + per-file data key）
- `core/pii.py`：6 类 PII 正则 + 替换（手机号 / 座机 / 身份证 / 银行卡 / 邮箱 / IP）
- `core/audit.py`：audit_logs 写入 helper（复用现有 `models/audit_log.py`）
- `api/dsar.py`：access + erasure 端点（admin only，写 audit_logs）
- 现有端点接入：reindex / recompute / prompt-activate 接入 audit_logs（Q2 quick win）
- 文件加密：`IngestionService` 写音频文件时 envelope 加密；`/dsar/access` 解密读

**WS-2 Eval 完整化**
- `api/eval.py`：4 端点（runs POST 启动 / GET 状态 / GET report / GET 列表）
- `eval/runner.py`：`RAGPipeline` 实装（替换 NotImplementedError）
- `eval/runner.py`：Position de-bias（原序 + 反序，2 次平均）
- `eval/state.py`：异步任务状态机（pending / running / succeeded / failed）+ APScheduler 复用
- `models/eval_run.py`（新表）：run_id / status / gold_set_path / config / aggregate_metrics / started_at / finished_at

**WS-3 rapidfuzz 实体聚类**
- `core/extractor.py`：实体合并阶段引入 `rapidfuzz.fuzz.WRatio`
- `models/entity_alias.py`（新表）：tenant_id / alias / canonical / created_by
- `prompts/entity_zh_parenting.md`：育儿咨询场景 prompt
- `prompts/versions.yaml`：注册 v1.1（parenting）
- `config.py`：`entity_fuzzy_threshold: float = 0.85`
- `eval/metrics/audio_graphy.py`：Entity F1 加 `fuzzy=False|True` 双模式

**3 Quick Wins（≤ 1 天 each）**
- Q1：repo 根目录 `.env.example` 增量（PIPL / rapidfuzz / metrics / cryptography 字段）
- Q2：`audit_logs` 写入在 reindex / recompute / prompt-activate（PIPL §3 子集）
- Q3：`prometheus_client` + `/metrics` endpoint（按服务划分）

### 1.4 Out of Scope

- ❌ MySQL TDE 配置（M6 仅落 config 文件 + docker-compose 注释段；实际启用 M7）
- ❌ 软删除回收站（M6 只做硬删除；软删除是 M7 议题）
- ❌ 中文姓名脱敏（M6 覆盖 6 类 PII；中文姓名姓氏库 M7+）
- ❌ 评估结果前端可视化（M7）
- ❌ Promptfoo / RAGAS / DeepEval 三方工具集成（M8）
- ❌ OSS 中文测试集加载器（AliMeeting / AISHELL-4 / WenetSpeech，M8）
- ❌ 评估指标支持中文 tokenizer 切换（M8，jieba / HanLP）
- ❌ admin CRUD 端点（M6 仅 DSAR；admin/tenants CRUD M7）
- ❌ 流式 ASR + 读写并发锁（Phase 4）
- ❌ Master AES key 自动轮换（M6 仅文档化，实施 M7+）

---

## 2. 核心用户故事

**US-1 合规官（启动 PIPL）**：开源用户部署 AudioGraphy，配置 `AUDIOGRAPHY_MASTER_KEY_PATH=/data/keys/master.key` 后，新录音自动加密落盘；过期录音自动硬删除；质检员看 transcript 默认 PII 已脱敏，需解密时走 `/dsar/access` 留审计。验收：上传一份含手机号的录音，14 天后 `recording_retention_days=14` 触发，文件被删；transcript 中 `13812345678` 显示为 `[REDACTED-PHONE]`。

**US-2 质检员（申请解密）**：质检员通过 `/dsar/access` 申请某条录音的明文 transcript，系统写 audit_log（actor / action=decrypt / target=recording_id / before=redacted / after=decrypted）。验收：admin 审批后返回明文；audit_logs 表有对应行。

**US-3 算法工程师（跑 Eval REST）**：工程师 POST 一个 gold set 到 `/api/v1/eval/runs`，后台异步跑 RAGPipeline + Position de-bias，前端轮询状态。验收：10 example × 2 次 = 20 次 LLM 调用；`GET /runs/{id}/report` 返回 Markdown；aggregate_metrics 含 8 项指标。

**US-4 算法工程师（prompt 升级 + 育儿场景）**：新增 `entity_zh_parenting.md` v1.1 prompt，通过 `/prompts/{id}/activate` 切到育儿场景；切换后跑 Eval 对比 v1.0 vs v1.1 的 Entity F1。验收：activate 写 audit_log；Eval report 含 fuzzy=True 模式 Entity F1。

**US-5 算法工程师（中文实体去重）**：录音含 `CS75 Plus` / `cs75plus` / `长安 CS75 Plus` 三种写法，rapidfuzz `WRatio ≥ 0.85` 自动归一到 `CS75 Plus`。验收：图里只有 1 个节点；Eval Entity F1 strict 模式 + fuzzy 模式双输出。

**US-6 运维（看 metrics）**：运维访问 `/metrics`，看到 `audiography_pipeline_duration_seconds` / `audiography_llm_call_total` / `audiography_cache_hit_total` 等指标。验收：`curl /metrics` 返回 200 + Prometheus 文本格式。

---

## 3. 需求池

### 3.1 P0（blocks release）

| ID | 描述 | 工作流 |
|----|------|--------|
| P0-1 | `core/retention.py` APScheduler 每日扫描 + 硬删除过期录音文件 | WS-1 |
| P0-2 | `core/crypto.py` AES-256-GCM envelope（cryptography.Fernet，master + data key） | WS-1 |
| P0-3 | `IngestionService` 写音频文件时 envelope 加密 | WS-1 |
| P0-4 | `core/pii.py` 6 类 PII 正则 + 替换 + 单元测试（happy + 边界 + 误报） | WS-1 |
| P0-5 | transcript 落库前过 PII 脱敏层（display-time redaction） | WS-1 |
| P0-6 | `core/audit.py` audit_logs 写入 helper（actor / action / target / before / after） | WS-1 |
| P0-7 | `api/dsar.py` access + erasure 端点（admin only + 写 audit_logs） | WS-1 |
| P0-8 | `config.py` 新增 `master_key_path` / `entity_fuzzy_threshold` / `metrics_port` 等字段 | WS-1/3 |
| P0-9 | `models/eval_run.py` 新表（run_id / status / gold_set_path / config / aggregate） | WS-2 |
| P0-10 | `eval/state.py` 异步任务状态机（pending / running / succeeded / failed） | WS-2 |
| P0-11 | `api/eval.py` 4 端点（POST runs / GET status / GET report / GET 列表） | WS-2 |
| P0-12 | `eval/runner.py` `RAGPipeline(EvalPipeline)` 实装（调 services/query.py） | WS-2 |
| P0-13 | `eval/runner.py` Position de-bias（原序 + 反序，2 次平均） | WS-2 |
| P0-14 | `core/extractor.py` 引入 rapidfuzz `fuzz.WRatio`（threshold=0.85 默认） | WS-3 |
| P0-15 | `models/entity_alias.py` 新表（tenant_id / alias / canonical / created_by） | WS-3 |
| P0-16 | `prompts/entity_zh_parenting.md`（育儿咨询场景 prompt） | WS-3 |
| P0-17 | `prompts/versions.yaml` 注册 v1.1（parenting） | WS-3 |
| P0-18 | `eval/metrics/audio_graphy.py` Entity F1 加 fuzzy=False\|True 双模式 | WS-3 |
| P0-19 | PIPL e2e 集成测试（上传含 PII 录音 → 加密落盘 → 脱敏展示 → DSAR 解密 → 过期删除） | WS-1 |
| P0-20 | Eval REST e2e 集成测试（POST run → 轮询 → report → 列表） | WS-2 |
| P0-21 | rapidfuzz 单元测试（Chinese near-dup + alias 表 + threshold 边界） | WS-3 |

### 3.2 Quick Wins（≤ 1 天 each）

| ID | 描述 | LOC | DESIGN § |
|----|------|-----|---------|
| Q1 | repo 根目录 `.env.example` 增量（M6 全字段 + M5 之前未补齐字段） | ~40 | §15.3 |
| Q2 | `audit_logs` 写入在 reindex / recompute / prompt-activate 3 类操作 | ~80 | §14.3 |
| Q3 | `prometheus_client` + `/metrics` endpoint（按服务划分 ~10 指标） | ~60 | §15.4 |

### 3.3 P1（可推迟 M7+）

- MySQL TDE 实际启用（M6 仅文档化 + compose 注释）
- 软删除回收站（recycling bin，30 天可恢复）
- 中文姓名脱敏（姓氏库 + 模糊匹配）
- 评估结果前端可视化（趋势图 + A/B 对比）
- admin CRUD 端点（/admin/tenants, /admin/users）
- Master AES key 自动轮换

### 3.4 P2（可推迟 M8+）

- OSS 中文测试集加载器（AliMeeting / AISHELL-4 / WenetSpeech）
- Promptfoo / RAGAS / DeepEval 三方工具集成
- 评估指标中文 tokenizer（jieba / HanLP）切换
- 声纹特征向量单独存储

---

## 4. PIPL §14.3 详细设计

> **本节为 WS-1 实现的 source of truth**。原则：**0 新业务依赖**（cryptography 是大牌包，已加入 deps）；保留期硬删除（不开源用户也能用）；master key 文件 + envelope（不引入 KMS / Vault，开源友好）。

### 4.1 保留期策略矩阵

`config.recording_retention_days`（已存在，默认 90）。

| 维度 | 选择 | 理由 |
|------|------|------|
| 触发频率 | **daily 03:00**（cron `0 3 * * *`） | 门店低峰，凌晨执行不影响业务；Q2 open question |
| Scope | `WHERE status='completed' AND recorded_at < NOW() - retention_days` | 不动 queued / failed 的录音 |
| Action | **硬删除音频文件**（保留 transcript / tags / segments / tag_facts / audit_logs） | 合规要求"原始声音数据需到期删除"；transcript 是加工产物不算 PII（已脱敏） |
| 软删除字段 | `Recordings.deleted_at` 新增列（M6 only sets to NULL；M7 接入软删除回收站） | 表结构前瞻，业务逻辑 M6 不消费 |
| Audit | 写 audit_log（action=`retention_delete`，target=recording_id，before=`{path}`，after=`{}`） | 必须可追溯 |
| 回滚 | 无（硬删除不可逆；如需恢复走备份） | 合规角度，删除应不可逆 |

**实现位置**：`backend/audio_graphy/core/retention.py`（新模块）。

```python
# scheduler.py lifespan 启动时注册
scheduler.add_job(
    run_retention_sweep,
    trigger=CronTrigger(hour=3, minute=0),
    id="retention_daily",
    coalesce=True,
    max_instances=1,
)
```

### 4.2 AES-256-GCM Envelope 加密流程

> **决策（locked）**：cryptography.Fernet envelope；master key 存文件（M6），不引入 Vault；ChaCha20-Poly1305 不考虑（社区熟悉度低）。

```
1. Master Key（per-deploy，存文件 0600 权限）
   - 32 bytes random，base64 编码
   - AUDIOGRAPHY_MASTER_KEY_PATH=/data/keys/master.key
   - 启动时 lazy load + cache

2. Per-file Data Key（每次加密生成）
   - Fernet.generate_key() → 32 bytes
   - 用 master key 加密 data key → encrypted_dk
   - 落盘：b"\xAG\x01" + len(encrypted_dk) + encrypted_dk + ciphertext

3. Nonce / IV：Fernet 内部处理（HMAC-SHA256 + AES-128-CBC internally）
```

**实现位置**：`backend/audio_graphy/core/crypto.py`（新模块，~150 LOC）。

```python
class AudioCrypto:
    """AES-256-GCM envelope encryption for audio files at rest."""
    def __init__(self, master_key_path: Path) -> None: ...
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...
```

**退化策略**：`master_key_path` 未配置 → 启动 warn 日志，加密功能 disabled，audio 明文落盘（仅 mock/dev 可接受；real 部署强制配置）。

### 4.3 PII 脱敏正则表 + 替换规则

> **决策（locked）**：M6 覆盖 6 类 PII（手机号 / 座机 / 身份证 / 银行卡 / 邮箱 / IP）；中文姓名脱敏 M7+。

| 类别 | 正则（Python flavor） | 替换 | 说明 |
|------|---------------------|------|------|
| 手机号 | `1[3-9]\d{9}` | `[REDACTED-PHONE]` | 11 位中国大陆手机号；前后需边界（非数字） |
| 座机 | `(?:0\d{2,3}-)?\d{7,8}` | `[REDACTED-LANDLINE]` | 区号-号码（如 010-12345678） |
| 身份证号 | `[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]\|1[0-2])(?:0[1-9]\|[12]\d\|3[01])\d{3}[\dXx]` | `[REDACTED-ID]` | 18 位，含校验位 |
| 银行卡号 | `\b[1-9]\d{15,18}\b` | `[REDACTED-CARD]` | 16-19 位数字，前缀非 0；可能误报长数字，权衡敏感性 |
| 邮箱 | `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` | `[REDACTED-EMAIL]` | 标准 RFC 简化版 |
| IP（v4） | `\b(?:25[0-5]\|2[0-4]\d\|[01]?\d\d?)\.(?:25[0-5]\|2[0-4]\d\|[01]?\d\d?){3}\b` | `[REDACTED-IP]` | 标准 IPv4 |

**实现位置**：`backend/audio_graphy/core/pii.py`（新模块，~120 LOC）。

```python
@dataclass(frozen=True)
class PIIRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str

PII_RULES: tuple[PIIRule, ...] = (
    PIIRule("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED-PHONE]"),
    PIIRule("landline", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"), "[REDACTED-LANDLINE]"),
    PIIRule("id_card", re.compile(r"..."), "[REDACTED-ID]"),
    # ...
)

def redact(text: str) -> str:
    """Apply all PII rules left-to-right; idempotent."""
    for rule in PII_RULES:
        text = rule.pattern.sub(rule.replacement, text)
    return text
```

**使用位置**：transcript 落库前过 `redact()`；display-time 也走同一函数（兜底）；明文版本仅在 DSAR access 后通过 `decrypt` 路径暴露。

### 4.4 audit_logs Schema 复用（已有 model）

`models/audit_log.py` 已就位（M3 建表未使用）。M6 复用：

| 字段 | 类型 | M6 用法 |
|------|------|---------|
| `id` | BigInt PK | — |
| `tenant_id` | str | 自动注入（TenantScopedBase） |
| `user_id` | BigInt FK→users.id（nullable） | 从 JWT 取 |
| `action` | str(64) | `reindex` / `recompute` / `prompt-activate` / `decrypt` / `delete` / `export` / `retention_delete` |
| `target` | str(255) | `<entity_type>:<entity_id>`（如 `recording:42` / `prompt:7`） |
| `before_value` | JSON | 操作前状态快照（如 `{"path": "/data/audio/x.wav"}`） |
| `after_value` | JSON | 操作后状态（如 `{"path": null}` 表示删除） |
| `occurred_at` | DateTime(tz) | `datetime.now(UTC)` |

**写入 helper**：`backend/audio_graphy/core/audit.py`（新模块，~80 LOC）。

```python
async def write_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: int | None,
    action: str,
    target: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append a row to audit_logs (fire-and-forget, never blocks business op)."""
    session.add(AuditLog(
        tenant_id=tenant_id, user_id=user_id, action=action, target=target,
        before_value=before, after_value=after, occurred_at=datetime.now(UTC),
    ))
    await session.flush()
```

**接入点（Q2 quick win）**：
- `POST /recordings/{id}/reindex` → action=`reindex`，target=`recording:{id}`
- `POST /tags/recompute` → action=`recompute`，target=`task:{task_id}`
- `POST /prompts/{id}/activate` → action=`prompt-activate`，target=`prompt:{id}`

**接入点（WS-1 PIPL）**：
- `POST /dsar/access` → action=`decrypt`，target=`recording:{id}`，before=`{displayed: "redacted"}`，after=`{displayed: "plain"}`
- `POST /dsar/erasure` → action=`delete`，target=`recording:{id}`，before=`{path: "/data/..."}`，after=`{}`
- retention sweep → action=`retention_delete`，target=`recording:{id}`

### 4.5 数据导出 API（DSAR）

GDPR-style access + erasure，PIPL 第 45-47 条落地。

| 方法 · 路径 | 角色 | 功能 |
|------------|------|------|
| `POST /api/v1/dsar/access` | admin | 申请某 recording 的明文 transcript（含未脱敏 PII）；写 audit_log；返回临时下载 URL |
| `POST /api/v1/dsar/erasure` | admin | 硬删除某 recording 的音频文件 + transcript（保留 tag_facts / audit_logs） |

**Request schema（access）**：

```json
{
  "recording_id": 42,
  "reason": "质检复盘需要"
}
```

**Response schema（access）**：

```json
{
  "request_id": "req-abc123",
  "recording_id": 42,
  "download_url": "/api/v1/dsar/files/req-abc123",
  "expires_at": "2026-07-21T15:00:00Z",
  "audit_log_id": 1024
}
```

**Open question Q5（locked）**：DSAR 不支持匿名请求，必须 admin 角色。

---

## 5. Eval REST API 完整契约

> **本节为 WS-2 实现的 source of truth**。原则：**APScheduler 复用现有 in-process worker**（不引入 Celery/RQ），异步任务状态机简单 4 态。

### 5.1 端点表

| 方法 · 路径 | 角色 | 功能 | DESIGN § |
|------------|------|------|---------|
| `POST /api/v1/eval/runs` | admin | 启动一次评估（gold set + pipeline 配置） | §8.1 / §12.1 |
| `GET /api/v1/eval/runs/{id}` | admin / inspector | 查单次评估状态 | §12.1 |
| `GET /api/v1/eval/runs/{id}/report` | admin / inspector | 下载 Markdown 报告 | §8.3 |
| `GET /api/v1/eval/runs` | admin / inspector | 列表（分页 + 过滤 status） | §12.1 |

### 5.2 请求 schema（POST /runs）

```json
{
  "gold_set_path": "examples/eval/smoke.yaml",
  "pipeline": "rag",                    // "rag" | "mock"
  "k": 5,
  "judge_llm": true,                    // 是否启用 LLM-as-judge
  "position_debias": true,              // §8.2 默认 true
  "metadata": {"scenario": "smoke"}
}
```

### 5.3 响应 schema

**POST /runs（201 Created）**：

```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "pending",
  "gold_set_path": "examples/eval/smoke.yaml",
  "started_at": "2026-07-21T14:32:11Z",
  "config": {
    "pipeline": "rag",
    "k": 5,
    "judge_llm": true,
    "position_debias": true
  },
  "poll_interval_seconds": 5
}
```

**GET /runs/{id}（200 OK）**：返回 status / progress `{completed, total}` / started_at / finished_at / aggregate_metrics（完成时填）/ errors。

**GET /runs/{id}/report（200 OK，text/markdown）**：完整 M5 §5.5 格式的 Markdown 报告（含 aggregate + per-example highlights）。

**GET /runs（200 OK）**：分页 + status 过滤；返回 `{runs: [...], total, page, page_size}`。

### 5.4 异步任务状态机

```
pending ──scheduler picks up──► running ──all examples done──► succeeded
                                    │
                                    │ pipeline crashed (no retry, fail fast)
                                    ▼
                                 failed
```

**实现**：
- `models/eval_run.py`（新表）：`run_id` / `status` / `gold_set_path` / `config_json` / `aggregate_metrics_json` / `started_at` / `finished_at` / `error_message`。
- `eval/state.py`：`EvalTaskStore` 包装 DB CRUD。
- `scheduler.py` lifespan 注册新 job：每 `eval_run_poll_seconds` 扫一次 `status=pending` 的 run，pickup → 跑 `EvalRunner` → 写回结果。

### 5.5 Position De-bias 实现细节

> **决策（locked）**：每个 query 跑 2 次（原序 + 反序）取平均；不开 3 次（成本/方差权衡）。

```python
class LLMJudge:
    async def score_with_position_debias(
        self, query: str, answer: str, gold_answer: str, context: list[str]
    ) -> float:
        """Run judge twice (original + reversed context order), average."""
        # 原序
        score_ori = await self._judge_once(query, answer, gold_answer, context)
        # 反序（打乱 retrieved context 顺序，消除位置偏差）
        reversed_context = list(reversed(context))
        score_rev = await self._judge_once(query, answer, gold_answer, reversed_context)
        return (score_ori + score_rev) / 2
```

**适用范围**：所有依赖 context 顺序的 LLM-as-judge 指标（faithfulness / answer_relevance / factual_correctness）；context_precision / context_recall 等 token-overlap 指标不需要（与顺序无关）。

**成本影响**：10 example × 2 次 = 20 次 LLM 调用（vs 不去偏的 10 次）；mock 模式 CI 全免费，real 模式开销可接受。

### 5.6 RAGPipeline 实装

```python
class RAGPipeline:
    """Real pipeline — calls services.QueryService for end-to-end retrieval + generation.

    Replaces the M5 NotImplementedError stub. Each predict() call:
        1. query_service.search(gold.query) → retrieved chunks + answer
        2. fetch entities / edges / tags from the graph + tag layer
        3. assemble PredictedResult
    """

    def __init__(
        self,
        query_service: QueryService,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        self._query = query_service
        self._graph = graph_store
        self._file_index = file_index

    async def predict(self, gold: GoldExample) -> PredictedResult:
        result = await self._query.search(
            query=gold.query, tenant_id=gold.metadata.get("tenant", "default"), top_k=5
        )
        entities = self._graph.get_entities_for_chunks([c.id for c in result.chunks])
        edges = self._graph.get_edges_for_entities([e.name for e in entities])
        tags = await self._file_index.get_tags_for_chunks([c.id for c in result.chunks])
        return PredictedResult(
            query=gold.query,
            answer=result.answer,
            retrieved_context_ids=tuple(c.id for c in result.chunks),
            entities=tuple((e.name, e.type) for e in entities),
            edges=tuple((s, r, d, c) for s, r, d, c in edges),
            tags=tuple(tags),
        )
```

---

## 6. rapidfuzz 实体聚类设计

> **本节为 WS-3 实现的 source of truth**。原则：**rapidfuzz `fuzz.WRatio`**（不是 token_set_ratio，中文不需要分词）；别名表存 DB（不存 YAML，可热改）。

### 6.1 fuzz.WRatio Threshold 配置

```python
# config.py 新增字段
entity_fuzzy_threshold: float = 0.85   # M6 default；Q3 open question
```

**threshold 决策矩阵**：

| Threshold | 行为 | 适用场景 |
|-----------|------|---------|
| 0.90（严格） | 只合并几乎相同的实体（如 `CS75 Plus` vs `CS75PLUS`） | 高精度场景（金融 / 法律） |
| **0.85（默认）** | 合并明显同一实体（如 `CS75 Plus` vs `长安 CS75 Plus`） | 门店质检默认 |
| 0.80（宽松） | 合并疑似同一（如 `UNI-V` vs `UNIV`） | 召回优先（图谱合并最大化） |

### 6.2 别名表 Schema（新表 entity_aliases）

> **决策（locked）**：DB-backed（tenant-scoped，可热改），不存 YAML。

```python
# models/entity_alias.py（新）
class EntityAlias(TenantScopedBase):
    """Entity alias → canonical name mapping.

    tenant-scoped: each tenant can have their own alias overrides.
    Hot-editable: changes take effect on next extraction (no service restart).
    """
    __tablename__ = "entity_aliases"

    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "alias", name="uq_entity_aliases_tenant_alias"),
        Index("ix_entity_aliases_tenant_canonical", "tenant_id", "canonical"),
    )
```

### 6.3 三层解析优先级

```
实体名称 → 归一化流程：
  Layer 1: DB 别名表（entity_aliases，tenant-scoped，可热改）  ← 优先级最高
       ↓ miss
  Layer 2: rapidfuzz fuzz.WRatio（threshold 0.85 默认）对已归一化实体集模糊匹配
       ↓ miss
  Layer 3: 硬编码 _DEFAULT_ALIASES（CS75 Plus / UNI-V 等兜底）
       ↓ miss
  原名保留
```

**实现位置**：`backend/audio_graphy/core/extractor.py:521`（`_normalize_entities` 方法升级）。

```python
def _normalize_entities(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
    normalised: list[ExtractedEntity] = []
    canonical_names: list[str] = []   # 已归一化的实体名（用于 fuzzy 比对）

    for ent in entities:
        # Layer 1: DB alias
        canonical = self._db_alias_lookup(ent.name)
        if canonical is None:
            # Layer 2: rapidfuzz
            canonical = self._fuzzy_match(ent.name, canonical_names, threshold=0.85)
        if canonical is None:
            # Layer 3: hardcoded
            canonical = self._aliases.get(ent.name, ent.name)

        if canonical == ent.name:
            canonical_names.append(canonical)

        normalised.append(replace(ent, name=canonical or ent.name))
    return normalised
```

### 6.4 育儿 Prompt 与汽车 Prompt 切换

`prompts/entity_zh.md`（v1.0，汽车销售）+ 新增 `prompts/entity_zh_parenting.md`（v1.1，育儿咨询）。

**v1.1 改动点（vs v1.0）**：

| 维度 | v1.0 汽车 | v1.1 育儿 |
|------|----------|----------|
| 实体类型 | 客户 / 坐席 / 车型 / 价格方案 / 金融政策 / 优惠权益 / 竞品 / 预约事件 | 家长 / 顾问 / 宝宝月龄 / 育儿问题 / 育儿方案 / 商品推荐 / 课程包 / 预约事件 |
| Few-shot | "坐席张敏向客户推荐 CS75 Plus..." | "顾问李老师向家长推荐 6-12 月辅食课程..." |
| 注意事项 | 不翻译中英混读车型名 | 识别月龄段（如 "6 月龄"、"2 岁"）作为实体 |

**`versions.yaml` 注册**：

```yaml
prompts:
  entity_zh:
    v1.0:
      file: entity_zh.md
      changelog: "初始版本，汽车销售领域，GraphRAG 分隔符协议 + few-shot + Gleaning"
      active: true                    # 默认仍汽车
  entity_zh:
    v1.1:
      file: entity_zh_parenting.md
      changelog: "育儿咨询场景（家长 / 顾问 / 月龄 / 课程包），与 v1.0 共存可 A/B"
      active: false
```

**切换逻辑**：`POST /prompts/{id}/activate` 已存在；切换后 `IngestionService` 加载新 prompt 时通过 `versions.yaml` 找文件路径，无须改代码。

### 6.5 Eval Entity F1 双模式

```python
# eval/metrics/audio_graphy.py 升级
def entity_f1(
    gold: GoldExample, pred: PredictedResult, *, fuzzy: bool = False
) -> MetricResult:
    """Entity F1 — strict (exact match) or fuzzy (rapidfuzz WRatio >= 0.85).

    Reports both in EvalRun.aggregate_metrics:
        - entity_f1_strict
        - entity_f1_fuzzy
    """
    if fuzzy:
        # 用 rapidfuzz 模糊匹配判断 TP/FP
        tp = sum(1 for g in gold.gold_entities if any(
            fuzz.WRatio(g[0], p[0]) >= 0.85 and g[1] == p[1]
            for p in pred.entities
        ))
    else:
        # 严格集合匹配（M5 行为保留）
        tp = len(set(gold.gold_entities) & set(pred.entities))
    # ...
```

**报告输出**：aggregate_metrics 同时输出 `entity_f1_strict` 和 `entity_f1_fuzzy`；前端（M7）展示对比图。

---

## 7. Prometheus Metrics 设计（Q3 Quick Win）

> **本节为 Q3 quick win 实现 source of truth**。LOC ~60；prometheus_client 依赖新增。

### 7.1 指标清单（按服务划分）

| 指标名 | 类型 | Labels | 来源 |
|-------|------|--------|------|
| `audiography_pipeline_duration_seconds` | Histogram | `tenant_id`, `stage` | VAD / ASR / chunk / extract / graph / tag |
| `audiography_pipeline_total` | Counter | `tenant_id`, `status` | success / failed |
| `audiography_llm_call_total` | Counter | `model`, `cached` | strong / weak / judge |
| `audiography_llm_call_duration_seconds` | Histogram | `model` | — |
| `audiography_cache_hit_total` | Counter | `layer` | layer1 / layer2 |
| `audiography_vector_query_duration_seconds` | Histogram | `tenant_id` | — |
| `audiography_tag_recompute_total` | Counter | `tenant_id`, `trigger` | manual / prompt_upgrade / late_arrival |
| `audiography_retention_deleted_total` | Counter | `tenant_id` | daily sweep |
| `audiography_audit_log_written_total` | Counter | `action` | 6 类敏感动作 |
| `audiography_dsar_requests_total` | Counter | `type`, `status` | access / erasure；success / denied |
| `audiography_eval_run_total` | Counter | `status` | pending / running / succeeded / failed |
| `audiography_eval_example_duration_seconds` | Histogram | — | per-example |

### 7.2 端点集成

```python
# main.py
from prometheus_client import make_asphony, CollectorRegistry
from prometheus_client import Counter, Histogram

REGISTRY = CollectorRegistry()
PIPELINE_DURATION = Histogram(
    "audiography_pipeline_duration_seconds",
    "Pipeline stage duration",
    ["tenant_id", "stage"], registry=REGISTRY,
)
# ...（共 ~12 指标）

app.add_route("/metrics", make_asphony(REGISTRY))
```

**Open question Q4（locked）**：复用 8000（主 app），不独立 9090（开源用户部署最简）。

---

## 8. .env.example 增量（Q1 Quick Win）

> Q1 是"补齐"，不仅 M6 新字段；同时把 M5 之前 `.env.example` 与 `config.py` 不一致的字段补齐。

### 8.1 M6 新增字段

```dotenv
# --- PIPL §14.3 (M6) ----------------------------------------------------
AUDIOGRAPHY_MASTER_KEY_PATH=/data/keys/master.key
# 留空则 PIPL 加密 disabled（仅 mock/dev 可接受；real 部署必须配置）

# --- Eval REST (M6) ----------------------------------------------------
EVAL_RUN_POLL_SECONDS=5              # scheduler 扫 pending eval run 的间隔

# --- Entity fuzzy matching (M6) ---------------------------------------
ENTITY_FUZZY_THRESHOLD=0.85          # rapidfuzz WRatio threshold；0.80 宽松 / 0.90 严格

# --- Prometheus metrics (M6) ------------------------------------------
METRICS_ENABLED=true                 # false → /metrics 端点返回 404
```

### 8.2 补齐字段（M5 之前漏写）

```dotenv
# --- Eval subsystem (M5 补齐) -----------------------------------------
JUDGE_LLM_MODEL=                     # empty = use LLM_STRONG_MODEL
EVAL_CONCURRENCY=4

# --- LLM (M4 补齐) -----------------------------------------------------
BCRYPT_ROUNDS=12
JWT_REFRESH_EXP_HOURS=84
```

---

## 9. 测试策略

### 9.1 WS-1 PIPL

| 用例 | 描述 |
|------|------|
| `test_pii_redact_{phone,id_card,landline,email,ip,bank_card}` | 6 类正则 happy path |
| `test_pii_redact_idempotent` | 两次 redact 不替换 `[REDACTED-*]` 标记 |
| `test_pii_redact_false_positive` | 长订单号 vs 银行卡号边界 |
| `test_crypto_roundtrip` / `test_crypto_wrong_master_key` | encrypt/decrypt 正反 |
| `test_retention_sweep` | 过期录音 → 文件删 + audit_log 写入 |
| `test_dsar_{access,erasure}_writes_audit` | DSAR + audit_log 联动 |
| `test_audit_log_helper` | write_audit 单元测试 |
| `test_pipl_e2e` | 集成：上传含 PII → 加密落盘 → 脱敏展示 → DSAR 解密 → 过期删除 |

### 9.2 WS-2 Eval REST

| 用例 | 描述 |
|------|------|
| `test_eval_run_{create,running,succeeded,failed,report,list}` | 4 端点 + 状态机 4 态 |
| `test_rag_pipeline_mock_query_service` | RAGPipeline 用 mock QueryService 跑通 |
| `test_position_debias_average` | 同 input 跑 2 次（原+反）取平均 |
| `test_position_debias_disable_flag` | `position_debias=false` → 只跑 1 次 |

### 9.3 WS-3 rapidfuzz

| 用例 | 描述 |
|------|------|
| `test_fuzzy_match_{exact,near_dup,chinese_prefix,below_threshold}` | WRatio 4 个边界 |
| `test_db_alias_priority` / `test_hardcoded_fallback` | 三层 fallback 优先级 |
| `test_entity_alias_crud` | tenant-scoped + uq 唯一约束 |
| `test_entity_f1_strict_vs_fuzzy` | 双模式同 example 都返回 |
| `test_parenting_prompt_loads` / `test_versions_yaml_v1_1_registered` | v1.1 prompt 加载 |

### 9.4 测试矩阵

| 模块 | 用例数 |
|------|--------|
| PIPL（pii + crypto + retention + dsar + audit） | 25 |
| Eval REST（4 endpoints + state machine + RAGPipeline + de-bias） | 28 |
| rapidfuzz（fuzzy match + alias table + Entity F1 dual mode + parenting prompt） | 18 |
| Prometheus metrics | 8 |
| .env.example schema 验证 | 5 |
| audit_logs quick win（reindex / recompute / prompt-activate） | 5 |
| **合计** | **89** |

### 9.5 覆盖率目标

| 模块 | 目标 |
|------|------|
| `core/pii.py` | ≥ 95% |
| `core/crypto.py` | ≥ 90%（不易测 InvalidToken） |
| `core/retention.py` | ≥ 90% |
| `core/audit.py` | ≥ 95% |
| `api/dsar.py` | ≥ 90% |
| `api/eval.py` | ≥ 90% |
| `eval/runner.py`（RAGPipeline + de-bias） | ≥ 85% |
| `core/extractor.py`（fuzzy 升级段） | ≥ 85% |
| `models/entity_alias.py` | ≥ 95% |

---

## 10. 验收标准

### 10.1 功能

- [ ] 上传含手机号 + 身份证号的录音，GET /recordings/{id} 返回的 transcript 中两类 PII 全部脱敏
- [ ] 配置 `AUDIOGRAPHY_MASTER_KEY_PATH` 后，新录音的音频文件是加密二进制；`head -c 4` 不是 RIFF/WAVE
- [ ] DSAR access 返回明文 transcript + audit_log 写入
- [ ] 设置 `recording_retention_days=0` + 触发 sweep，录音文件不存在；transcript / tag_facts 仍在
- [ ] `POST /api/v1/eval/runs` 返回 run_id；轮询至 succeeded；aggregate_metrics 含 8 指标
- [ ] `GET /api/v1/eval/runs/{id}/report` 返回 text/markdown
- [ ] `RAGPipeline` 跑通 ≥ 1 个 example（用 mock QueryService）
- [ ] Position de-bias 跑 2 次（可通过 LLM call count 验证）
- [ ] `CS75 Plus` / `CS75PLUS` / `长安 CS75 Plus` 三种写法归一到 1 个图节点
- [ ] activate v1.1 prompt 后新录音用育儿 prompt 抽取（实体类型含"家长"/"月龄"等）
- [ ] `curl /metrics` 返回 200 + Prometheus 文本格式
- [ ] `pytest backend/tests/ -x` 全绿，总数 ≥ **840**（751 + 89）
- [ ] `ruff check backend/` 0 错；`mypy backend/audio_graphy/{core,api,eval}/` 0 错

### 10.2 代码质量

- [ ] 新增模块单文件 ≤ 250 LOC（除 `core/crypto.py` 可到 200 + `api/eval.py` 可到 250）
- [ ] 无硬编码 master key / PII 正则 / threshold（全部走 config.py）
- [ ] 关键 docstring 中英双语（DSAR / PIPL / rapidfuzz 相关）
- [ ] 所有 6 类敏感动作都写 audit_log（grep `write_audit(` ≥ 6 处）
- [ ] master key 文件权限 0600（启动时检查 + warn）

### 10.3 文档

- [ ] `docs/m6-prd.md`（本文件）≤ 900 行
- [ ] `docs/deployment.md` 增 PIPL 启动指引（master key 生成 + retention 配置 + DSAR 流程）
- [ ] `.env.example` 覆盖所有 M6 新字段（含注释说明每项含义）
- [ ] `README.md` 加 M6 状态说明（≤ 10 行）：PIPL 端到端 + Eval REST + rapidfuzz
- [ ] `prompts/entity_zh_parenting.md` 含完整 system prompt + few-shot + Gleaning

### 10.4 向后兼容

- [ ] M5 既有 `.env`（无 `AUDIOGRAPHY_MASTER_KEY_PATH`）启动不报错，仅 warn 日志，加密 disabled
- [ ] M5 API 端点行为不变（`/recordings` / `/query` / `/tags/*` 等）
- [ ] M5 CLI `python -m audio_graphy.eval` 仍可用（REST 是新增通道，CLI 不删）
- [ ] M5 entity_zh.md v1.0 默认 active 不变（v1.1 需手动 activate）

---

## 11. 待确认问题（≤ 5 个）

### Q1（高）· Master AES key 存哪里？

M6 默认文件（`AUDIOGRAPHY_MASTER_KEY_PATH`，0600 权限）。**选项**：(a) 环境变量 base64 / (b) Docker secret / (c) Vault / (d) 文件（M6 默认）。**默认 (d)，需确认。** Vault 是 M7+ 议题。

### Q2（中）· PIPL 保留期触发频率？

M6 默认 daily 03:00 cron。**选项**：(a) daily / (b) hourly / (c) per-request lazy（每次 GET /recordings 时检查）。**默认 (a)，需确认。** Lazy 模式延迟最低但 audit 不连续。

### Q3（中）· rapidfuzz threshold 默认值？

M6 默认 0.85。**选项**：(a) 0.85（默认） / (b) 0.80（召回优先） / (c) 0.90（精度优先）。**默认 (a)，需确认。** 不同门店业务可能要求不同；tenant-scoped config 是 M7+ 议题。

### Q4（低）· Prometheus 端口？

M6 默认复用 8000（主 app），通过 `/metrics` 路径暴露。**选项**：(a) 复用 8000（M6 默认） / (b) 独立 9090（标准 Prometheus 端口）。**默认 (a)，需确认。** 复用减少部署复杂度；独立端口利于网络策略隔离。

### Q5（低）· DSAR 是否支持匿名请求？

**M6 locked：否（必须 admin 角色）**。DSAR 涉及 PII 明文暴露，必须强认证 + 审计。inspector 角色 M7 可考虑（限制只能看自己负责的 recording）。

---

## 附录 · 交付物清单

| 文件 | 状态 | 估算行数 | 工作流 |
|------|------|---------|--------|
| `backend/audio_graphy/core/pii.py` | 新增 | 120 | WS-1 |
| `backend/audio_graphy/core/crypto.py` | 新增 | 200 | WS-1 |
| `backend/audio_graphy/core/retention.py` | 新增 | 150 | WS-1 |
| `backend/audio_graphy/core/audit.py` | 新增 | 80 | WS-1/Q2 |
| `backend/audio_graphy/api/dsar.py` | 新增 | 180 | WS-1 |
| `backend/audio_graphy/services/ingestion.py` | 改 | +30 / -5 | WS-1 |
| `backend/audio_graphy/services/query.py` | 改 | +20 / -5 | WS-2 |
| `backend/audio_graphy/models/eval_run.py` | 新增 | 80 | WS-2 |
| `backend/audio_graphy/models/entity_alias.py` | 新增 | 60 | WS-3 |
| `backend/audio_graphy/eval/state.py` | 新增 | 100 | WS-2 |
| `backend/audio_graphy/eval/runner.py` | 改 | +120 / -10 | WS-2 |
| `backend/audio_graphy/api/eval.py` | 新增 | 250 | WS-2 |
| `backend/audio_graphy/core/extractor.py` | 改 | +80 / -20 | WS-3 |
| `backend/audio_graphy/prompts/entity_zh_parenting.md` | 新增 | 80 | WS-3 |
| `backend/audio_graphy/prompts/versions.yaml` | 改 | +5 | WS-3 |
| `backend/audio_graphy/eval/metrics/audio_graphy.py` | 改 | +40 / -10 | WS-3 |
| `backend/audio_graphy/api/metrics.py` | 新增 | 60 | Q3 |
| `backend/audio_graphy/main.py` | 改 | +15 / -2 | WS-1/Q3 |
| `backend/audio_graphy/scheduler.py` | 改 | +30 / -5 | WS-1/WS-2 |
| `backend/audio_graphy/config.py` | 改 | +20 | WS-1/3/Q3 |
| `backend/audio_graphy/api/recordings.py` | 改 | +5 | Q2 |
| `backend/audio_graphy/api/tags.py` | 改 | +5 | Q2 |
| `backend/audio_graphy/api/prompts.py` | 改 | +5 | Q2 |
| `.env.example` | 改 | +30 | Q1 |
| `backend/pyproject.toml` | 改 | +3 | WS-1/3/Q3 |
| `backend/alembic/versions/{ts}_m6_pipl_eval_rapidfuzz.py` | 新增 | 120 | WS-1/2/3 |
| `backend/tests/core/test_pii.py` | 新增 | 180 | WS-1 |
| `backend/tests/core/test_crypto.py` | 新增 | 120 | WS-1 |
| `backend/tests/core/test_retention.py` | 新增 | 100 | WS-1 |
| `backend/tests/core/test_audit.py` | 新增 | 80 | WS-1 |
| `backend/tests/api/test_dsar.py` | 新增 | 150 | WS-1 |
| `backend/tests/integration/test_pipl_e2e.py` | 新增 | 200 | WS-1 |
| `backend/tests/api/test_eval_runs.py` | 新增 | 250 | WS-2 |
| `backend/tests/eval/test_rag_pipeline.py` | 新增 | 120 | WS-2 |
| `backend/tests/eval/test_position_debias.py` | 新增 | 80 | WS-2 |
| `backend/tests/core/test_extractor_fuzzy.py` | 新增 | 150 | WS-3 |
| `backend/tests/models/test_entity_alias.py` | 新增 | 80 | WS-3 |
| `backend/tests/eval/test_entity_f1_fuzzy.py` | 新增 | 60 | WS-3 |
| `backend/tests/api/test_metrics.py` | 新增 | 60 | Q3 |
| `backend/tests/api/test_audit_quick_wins.py` | 新增 | 80 | Q2 |
| `docs/deployment.md` | 改 | +60 / -10 | WS-1 |
| `README.md` | 改 | +10 | — |
| `docs/m6-prd.md`（本文件） | 新增 | ≤ 900 | — |
| **总计 M6 增量** | — | **≤ 3500 行** | — |

---

**END OF M6 PRD** — 主理人确认 Q1–Q5 后即可进入架构（高见远）。
