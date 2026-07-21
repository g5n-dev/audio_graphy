# AudioGraphy M5 PRD — funASR Adapter + Evaluation Subsystem（Code-Ready）

| 字段 | 值 |
|------|-----|
| 版本 | v5.0.0-draft |
| 作者 | 许清楚（PM / AI 代行） |
| 主理人 | 齐活林 |
| 日期 | 2026-07-21 |
| 前置 | M4 commit `56674d9`（657 测试 / 91.46% 覆盖率） |
| 范围 | Code-Ready（写代码 + compose + 测试 + eval CLI，不拉起真实服务） |
| 工作流 | WS-1 funASR Adapter（解锁 M4 validator 约束）／ WS-2 Evaluation Subsystem（Gap Audit W18） |

---

## 1. 产品目标 + 范围边界

### 1.1 北极星

**让社区用户跑得起来、信得过 AudioGraphy**：

1. **跑得起来** — 拿到 GPU 机器后 5 分钟内启用 funASR 真实 ASR，无需改业务层。
2. **信得过** — 仓库自带评估工具链与黄金集范例，任何人能在 CI 中复现指标；开源发布前自证质量。

### 1.2 量化目标

- ✅ `Settings(adapter_asr_mode="real")` 不再 raise（M4 约束解锁）
- ✅ `FunASRAdapter` 满足 `protocols.ASRAdapter` 契约，与 `SileroVADAdapter` 同结构
- ✅ 评估 CLI `python -m audio_graphy.eval` 跑通 ≥ 1 个 example gold set
- ✅ 实现 5 项核心 RAG 指标 + 3 项 AudioGraphy 特异指标（in-tree，无新 pip 依赖）
- ✅ respx 测试覆盖 funASR adapter ≥ 90%
- ✅ 评估指标单元测试 ≥ 95%（纯函数，易于覆盖）
- ✅ M4 全部 657 测试 0 回归，总数 ≥ **700**

### 1.3 In Scope

**WS-1 funASR Adapter**
- `adapters/real/funasr.py`：OpenAI 兼容 `/v1/audio/transcriptions` 实现
- `adapters/exceptions.py`：新增 ASR 异常层级（`ASRAdapterError` + 4 子类）
- `config.py`：移除 M4 ASR-real validator 拒绝 + 新增 `FUNASR_URL` / `FUNASR_MODEL` 字段
- `bundle.py`：`build_hybrid_bundle` 中 ASR 分支切到 `FunASRAdapter`
- `docker-compose.yml`：funASR 镜像替换为官方 `funasr/server`，加 healthcheck
- respx 测试：funASR adapter 7 用例

**WS-2 Evaluation Subsystem**
- `audio_graphy/eval/`：新子包（数据模型 + 指标 + 报告器 + CLI）
- 5 项 RAG 指标：Context Precision@k / Context Recall / Faithfulness / Answer Relevance / Factual Correctness
- 3 项 AudioGraphy 特异指标：Entity F1 / Edge Precision×Confidence / Tag Accuracy
- LLM-as-judge：复用 `LLMOpenAIAdapter`（strong），无新 pip 依赖
- Gold set YAML schema + ≥ 10 条 example
- CLI 输出 Markdown + JSON 双格式报告
- 单元测试 + 黄金集 fixture

### 1.4 Out of Scope

- ❌ 真实拉起 funASR GPU 服务（仍 code-ready）
- ❌ 引入 ragas / ares / trulens / deepEval 等第三方评估库（齐活林 locked）
- ❌ 评估结果持久化到 MySQL（M6+，先落本地文件）
- ❌ 评估报告前端可视化（M6+）
- ❌ Prometheus metrics 暴露（Q5，先 reporter 输出文件）
- ❌ ASR 说话人分离 / 情感识别（M6+）
- ❌ 流式 ASR（M7+）

---

## 2. 核心用户故事

**US-1 开源集成者（启用 ASR）**：拿到 GPU 机器后改 `ADAPTER_ASR_MODE=real` 即可启用 funASR，业务层零改动。验收：`docker compose --profile real up` 后，上传音频能拿到真实转写文本，不再返回 MockASRAdapter 的固定脚本。

**US-2 CI 流水线（跑评估）**：无 GPU 环境下，CI 能跑评估黄金集 + 全部 adapter 测试。验收：`pytest backend/tests/eval/` + `python -m audio_graphy.eval --gold-set examples/eval/smoke.yaml --mock` 全绿。

**US-3 质检员（生成报告）**：质检员拉一份录音跑 RAG 评估，得到 Markdown 报告（含 8 项指标 + 失败 case 摘要）。验收：`python -m audio_graphy.eval --gold-set my.yaml --report-dir reports/` 在 `reports/` 下生成 `eval-<timestamp>.md` + `eval-<timestamp>.json`。

**US-4 开源贡献者（添加指标）**：新指标 in-tree 实现，单元测试覆盖公式。验收：在 `audio_graphy/eval/metrics/` 新增一个 `.py` + 对应 `test_*.py`，PR 模板有评估指标 checklist。

---

## 3. 需求池

### 3.1 P0（blocks release）

| ID | 描述 | 工作流 |
|----|------|--------|
| P0-1 | `adapters/real/funasr.py` 实现 `FunASRAdapter` | WS-1 |
| P0-2 | `adapters/exceptions.py` 新增 ASR 异常层级 | WS-1 |
| P0-3 | `config.py` 移除 ASR-real validator 拒绝 + 新增 funASR 配置字段 | WS-1 |
| P0-4 | `bundle.py` ASR 分支切到 `FunASRAdapter` | WS-1 |
| P0-5 | `docker-compose.yml` funASR 服务换官方镜像 + healthcheck | WS-1 |
| P0-6 | respx 测试：funASR adapter 7 用例（happy/4xx/5xx/timeout/bad_json/segments 解析） | WS-1 |
| P0-7 | `audio_graphy/eval/` 子包骨架（types + runner + cli + reporter） | WS-2 |
| P0-8 | 5 项 RAG 指标实现（in-tree，纯函数） | WS-2 |
| P0-9 | 3 项 AudioGraphy 特异指标（Entity F1 / Edge P/C / Tag Acc） | WS-2 |
| P0-10 | LLM-as-judge prompt 模板（fact extraction / faithfulness / answer relevance） | WS-2 |
| P0-11 | Gold set YAML schema + 10 条 example | WS-2 |
| P0-12 | CLI `python -m audio_graphy.eval`（Markdown + JSON 报告） | WS-2 |
| P0-13 | 指标单元测试 + 黄金集 fixture + LLM-as-judge mock 测试 | WS-2 |
| P0-14 | `docs/m5-eval.md`：评估指标公式 + gold set 字段说明 + 示例 | WS-2 |
| P0-15 | `docs/deployment.md` 增 funASR 启动指引 | WS-1 |

### 3.2 P1（可推迟 M6+）

- 评估结果持久化到 MySQL（`eval_runs` / `eval_results` 表）
- 评估报告前端可视化（趋势图）
- Prometheus metrics 暴露（Q5）
- 评估指标支持中文 tokenizer 切换（Q3）
- funASR 镜像 SHA 固定（Q1）
- LLM-as-judge 模型可配置（Q2，strong vs separate judge）

---

## 4. funASR HTTP 契约

> **本节为 WS-1 实现的 source of truth**。funASR-server 2026 版起官方支持 OpenAI 兼容 HTTP，废弃旧 WS:10095 二进制协议。

### 4.1 端点

**POST** `{FUNASR_URL}/v1/audio/transcriptions`

- Content-Type：`multipart/form-data`
- 与 `SileroVADAdapter` 上传音频同模式；与 `LLMOpenAIAdapter` 共享 OpenAI schema 风格

### 4.2 请求字段（multipart/form-data）

| Field | Type | Required | Default | 说明 |
|-------|------|----------|---------|------|
| `file` | file (wav/mp3/flac/m4a) | ✅ | — | 音频文件句柄 |
| `model` | string | ✅ | — | e.g. `fun-asr-nano` / `fun-asr-large`，需与服务端 `--served-model-name` 对齐 |
| `language` | string | ❌ | `zh` | BCP-47（`zh` / `en` / `ja`） |
| `response_format` | string | ❌ | `verbose_json` | M5 仅支持 `verbose_json`（含 segments + duration）；`json` / `text` 拒绝 |
| `temperature` | float | ❌ | `0.0` | 0 = greedy |
| `timestamp_granularities[]` | string[] | ❌ | `["segment"]` | `segment` / `word`；M5 仅消费 segment |

### 4.3 响应 schema（`response_format=verbose_json`）

```json
{
  "text": "今天我们讨论三个议题。",
  "segments": [
    {"id": 0, "start": 1.7, "end": 5.5, "text": "今天我们讨论三个议题。", "confidence": 0.96}
  ],
  "language": "zh",
  "duration": 12.1,
  "model": "fun-asr-nano"
}
```

### 4.4 状态码 → 异常映射

| HTTP / 异常源 | 异常类 | 日志级别 |
|---------------|--------|---------|
| 200 OK | `ASRResult(text, segments, language, duration)` | DEBUG |
| 400 | `ASRRequestError` | WARNING |
| 401 / 403 | `ASRAuthError`（funASR 启用 token 时） | ERROR |
| 413 | `ASRTooLargeError` | WARNING |
| 422 | `ASRRequestError`（不支持 response_format 等） | WARNING |
| 429 | `ASRRateLimitError`（M5 不重试，沿用 M4 LLM 策略） | WARNING |
| 5xx | `ASRServerError` | ERROR |
| `httpx.TimeoutException` | `ASRTimeoutError` | WARNING |
| `httpx.HTTPError`（其他） | `ASRServerError` | ERROR |
| 响应非 JSON / 缺 `text` | `ASRServerError` | ERROR |

### 4.5 默认参数

| 参数 | 默认值 | 理由 |
|------|--------|------|
| `timeout_sec` | `120.0` | ASR 比 VAD 慢，长音频 60s+ |
| `max_connect_sec` | `5.0` | 服务下线快速失败 |
| `max_connections` | `4` | ASR 单卡并发低，避免 OOM |
| `max_keepalive` | `2` | 同上 |

### 4.6 curl 示例

```bash
curl -X POST http://funasr:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer dummy" \
  -F "file=@audio.wav" \
  -F "model=fun-asr-nano" \
  -F "response_format=verbose_json" \
  -F "language=zh"
```

---

## 5. Evaluation 子系统设计

> **本节为 WS-2 实现的 source of truth**。原则：**不引入新 pip 依赖**，5 项 RAG 指标 + 3 项 AudioGraphy 特异指标全部 in-tree 实现，LLM-as-judge 复用 `LLMOpenAIAdapter(strong)`。

### 5.1 数据模型（`audio_graphy/eval/types.py`）

```python
@dataclass(frozen=True, slots=True)
class GoldExample:
    """One QA-style eval example."""
    query: str
    gold_answer: str
    gold_context_ids: tuple[str, ...]      # ground-truth chunk IDs (retrieval)
    gold_entities: tuple[tuple[str, str], ...]  # (entity_text, entity_type)
    gold_edges: tuple[tuple[str, str, str, EdgeConfidence], ...]  # (src, rel, dst, conf)
    gold_tags: tuple[dict[str, str], ...]  # {"tag_path": "...", "value": "..."}
    recording_id: str | None = None        # optional end-to-end audio
    metadata: dict[str, str] = field(default_factory=dict)  # tenant / scenario / etc

@dataclass(frozen=True, slots=True)
class PredictedResult:
    """Pipeline output for a single GoldExample."""
    query: str
    answer: str
    retrieved_context_ids: tuple[str, ...]    # in rank order
    entities: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str, str, EdgeConfidence], ...]
    tags: tuple[dict[str, str], ...]

@dataclass(frozen=True, slots=True)
class MetricResult:
    name: str
    value: float           # in [0.0, 1.0] unless otherwise noted
    denominator: int       # for aggregation (micro/macro)
    details: dict[str, float | int | str] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class EvalExampleResult:
    """All metrics for one (GoldExample, PredictedResult) pair."""
    example_id: str
    metrics: tuple[MetricResult, ...]
    error: str | None = None  # non-None if pipeline crashed for this example

@dataclass(frozen=True, slots=True)
class EvalRun:
    """One full evaluation run."""
    run_id: str               # UUID
    gold_set_path: str
    started_at: str           # ISO 8601
    finished_at: str
    config: dict[str, str]    # snapshot of relevant Settings (model names, etc.)
    aggregate_metrics: dict[str, float]  # mean across examples
    per_example: tuple[EvalExampleResult, ...]
```

### 5.2 Gold Set YAML Schema

`examples/eval/<name>.yaml`：

```yaml
- query: "CS75 Plus 七月优惠多少？"
  gold_answer: "5 万元现金优惠 + 2 年免息分期"
  gold_context_ids: ["chunk-001", "chunk-004"]
  gold_entities:
    - ["CS75 Plus", "车型"]
    - ["5万", "价格方案"]
  gold_edges:
    - ["坐席", "推荐", "CS75 Plus", "EXTRACTED"]
    - ["CS75 Plus", "搭配", "2年免息", "INFERRED"]
  gold_tags:
    - {tag_path: "接待.价格.优惠", value: "5万"}
    - {tag_path: "接待.金融.免息", value: "2年"}
  recording_id: "rec-2026-07-15-001"   # optional
  metadata: {scenario: "sales", tenant: "default"}

- query: "..."
  # ...
```

### 5.3 指标定义

> 所有指标范围 `[0.0, 1.0]`，越接近 1 越好；`Aggregate` 为所有 example 的算术平均。

#### 5.3.1 RAG 标准指标（5 项）

| 指标 | 公式 | 说明 |
|------|------|------|
| **Context Precision@k** | `(# gold_context_ids ∩ retrieved[:k]) / min(k, len(gold))` | k 默认 5。完美 = gold 全在 top-k。 |
| **Context Recall** | `(# gold_context_ids ∩ retrieved_all) / len(gold_context_ids)` | gold 是否被检索到（任意位置）。 |
| **Faithfulness** | `(supported_facts) / (total_facts_in_answer)` | LLM-as-judge 判定 answer 中每个事实是否能在 retrieved context 找到支持。反幻觉核心指标。 |
| **Answer Relevance** | LLM-as-judge 打分（0/0.5/1） | answer 是否直接回应 query（不跑题、不啰嗦）。 |
| **Factual Correctness** | `F1(precision, recall)` over fact sets | fact = LLM-as-judge 从 answer / gold_answer 抽取的最小事实单元。 |

**AudioGraphy 特异点**：默认 `k=5`（门店 RAG 通常 top-5 已覆盖答案段落）；fact 抽取 prompt 用中文 few-shot（M5 prompt 见 §5.4）。

#### 5.3.2 AudioGraphy 特异指标（3 项）

| 指标 | 公式 | 说明 |
|------|------|------|
| **Entity F1** | `F1` over `(entity_text, entity_type)` set | 实体抽取质量。Q3 决定 tokenizer；M5 默认按字符（无分词），可后续切换 jieba/HanLP。 |
| **Edge Precision×Confidence** | 按 `EdgeConfidence` 分层：`P_EXTRACTED = TP_EXTRACTED / (TP_EXTRACTED + FP_EXTRACTED)`；同理 INFERRED / AMBIGUOUS | 图谱构建质量。报告时三个子指标并列输出，外加 `macro_edge_precision`。 |
| **Tag Accuracy** | `(# tags where gold.tag_path==pred.tag_path AND value match) / len(gold_tags)` | 标签子系统准确率。value match 用字符串归一化（trim + lowercase + 全角半角）。 |

#### 5.3.3 数值范围 & 边界

- 分母为 0 时返回 `0.0`（约定，不抛错；`details.denominator_zero=True` 标记）
- Faithfulness：answer 为空 → 0.0；retrieved 为空 → 0.0
- Entity F1：gold 与 pred 都为空 → 返回 1.0（双方都没抽取，视为正确）

### 5.4 LLM-as-judge Prompt 模板

存放在 `audio_graphy/eval/prompts/`（与 `audio_graphy/prompts/` 区分，避免污染业务 prompt）。

#### 5.4.1 Fact Extraction Prompt（`extract_facts.txt`）

```
你是一个事实抽取助手。从下面这段中文文本中抽取所有原子事实（独立可验证的最小事实单元）。
每行一个事实，用简洁的中文陈述句表达。

【文本】
{text}

【输出格式】
- <事实 1>
- <事实 2>
- ...
```

#### 5.4.2 Faithfulness Judgment（`judge_faithfulness.txt`）

```
你是 RAG 系统的审核员。判断下面每个事实是否被【上下文】支持。

【上下文】
{context}

【待判断的事实】（编号 1..N）
{numbered_facts}

【输出格式】每行一个 JSON：{"id": 1, "supported": true|false}
仅输出 JSON，不要解释。
```

#### 5.4.3 Answer Relevance（`judge_relevance.txt`）

```
判断【回答】对【问题】的相关性，从 {0, 0.5, 1.0} 中选一：
- 1.0：直接回答问题，无冗余
- 0.5：部分相关 / 含跑题内容
- 0.0：完全无关

【问题】{query}
【回答】{answer}

仅输出一个数字。
```

### 5.5 报告格式（Markdown + JSON 双输出）

**JSON**：序列化 `EvalRun` dataclass，含 `aggregate_metrics` + 完整 `per_example`（含原始 retrieved / answer / entities / edges / tags）。

**Markdown** 示例：

```markdown
# Eval Report — `examples/eval/smoke.yaml`

- **Run ID**: `a1b2c3`
- **Started**: 2026-07-21 14:32:11
- **Finished**: 2026-07-21 14:33:47
- **Examples**: 10
- **Errors**: 0

## Aggregate Metrics

| Metric | Value |
|--------|-------|
| Context Precision@5 | 0.820 |
| Context Recall | 0.910 |
| Faithfulness | 0.875 |
| Answer Relevance | 0.900 |
| Factual Correctness (F1) | 0.742 |
| Entity F1 | 0.681 |
| Edge Precision (EXTRACTED) | 0.833 |
| Edge Precision (INFERRED) | 0.500 |
| Edge Precision (AMBIGUOUS) | 1.000 |
| Tag Accuracy | 0.940 |

## Per-Example Highlights

### Lowest Faithfulness

| example_id | query | faithfulness | notes |
|------------|-------|--------------|-------|
| ex-007 | "UNI-V 金融方案？" | 0.40 | 3/5 facts unsupported (answer 含黄金集外信息) |

### Errors (if any)

(none)
```

### 5.6 CLI 入口

```bash
python -m audio_graphy.eval \
  --gold-set examples/eval/smoke.yaml \
  --report-dir reports/ \
  [--mock]                  # 用 MockLLMAdapter 跑 LLM-as-judge（CI）
  [--k 5]                   # Context Precision top-k
  [--tenant default]
```

- `--mock`：CI / 本地无 GPU 时用 MockLLMAdapter 作为 judge（结果仅用于回归测试，不反映真实质量）
- 输出文件名：`eval-<run_id>.md` + `eval-<run_id>.json`
- 退出码：成功 0；任一 example 抛错且 `--strict` 则非 0

### 5.7 Runner 流程

```
EvalRunner.run(gold_set_path)
 ├─ load_gold_set(path) -> list[GoldExample]
 ├─ for each example:
 │    ├─ PredictedResult = pipeline.answer(example.query)
 │    │   (调 services.QueryService 或直接走 services.RetrievalService + GenerationService)
 │    ├─ for each metric:
 │    │    result = metric.compute(example, predicted, judge_llm=strong_llm)
 │    └─ EvalExampleResult(metrics, error=None)
 ├─ aggregate (arithmetic mean)
 └─ EvalRun(...) → write JSON + Markdown
```

---

## 6. config.py / bundle.py 改动

### 6.1 `config.py`

#### 6.1.1 移除 M4 ASR 拒绝（`config.py:165-169`）

```python
# ===== BEFORE =====
if self.adapter_asr_mode == "real":
    raise ValueError(
        "ADAPTER_ASR_MODE=real is not supported in M4 (funASR lands in M5)"
    )

# ===== AFTER =====
# (deleted — M5 unblocks ASR real)
```

#### 6.1.2 新增字段

```python
# funASR (M5 — used when adapter_asr_mode == "real")
funasr_url: str = "http://funasr:8000"           # OpenAI-compat 端点（不再是 :10095）
funasr_model: str = "fun-asr-nano"                # 默认 nano（CPU 友好）；GPU 可改 fun-asr-large
funasr_language: str = "zh"                       # BCP-47
funasr_timeout_sec: float = 120.0                 # ASR 长音频需要更宽松
```

#### 6.1.3 validator 增加 funASR URL sanity

```python
# M5 — URL sanity for funASR（沿用 M4 模式，warn-only）
if self.adapter_asr_mode == "real":
    for f in ("funasr_url",):
        u = getattr(self, f)
        if not u.startswith(("http://", "https://")):
            logger.warning("Field %s=%r is not http(s)://", f, u)
```

### 6.2 `bundle.py` — `build_hybrid_bundle` ASR 分支

```python
# ===== BEFORE (M4) =====
# ASR — always mock in M4 (validator already rejects real)
asr: ASRAdapter = MockASRAdapter(flaky=settings.mock_asr_flaky)

# ===== AFTER (M5) =====
if settings.adapter_asr_mode == "real":
    from audio_graphy.adapters.real.funasr import FunASRAdapter
    asr: ASRAdapter = FunASRAdapter(
        url=settings.funasr_url,
        model=settings.funasr_model,
        language=settings.funasr_language,
        timeout=settings.funasr_timeout_sec,
    )
else:
    asr = MockASRAdapter(flaky=settings.mock_asr_flaky)
```

### 6.3 `.env.example` 改动

```dotenv
# ===== BEFORE (M4) =====
ADAPTER_ASR_MODE=mock               # M4: must be mock (funASR lands in M5)
FUNASR_URL=http://funasr:10095

# ===== AFTER (M5) =====
ADAPTER_ASR_MODE=mock               # M5: set to "real" to enable funASR
FUNASR_URL=http://funasr:8000       # OpenAI-compat endpoint (NOT legacy :10095)
FUNASR_MODEL=fun-asr-nano           # options: fun-asr-nano (CPU) / fun-asr-large (GPU)
FUNASR_LANGUAGE=zh
FUNASR_TIMEOUT_SEC=120
```

---

## 7. docker-compose 改动

### 7.1 funASR 服务替换（`docker-compose.yml:285-296`）

```yaml
# ===== BEFORE (M4) =====
funasr:
  image: registry.cn-hangzhou.aliyuncs.com/funasr_recog/funasr-runtime-sdk-online-cpu-0.1.12
  container_name: audiography-funasr
  profiles: ["real"]
  restart: unless-stopped
  ports:
    - "10095:10095"
  # No healthcheck in M4

# ===== AFTER (M5) =====
funasr:
  # Official funASR server image with OpenAI-compatible HTTP API.
  # Image tag: see Q1 (latest vs SHA pin). Default: funasr/server:latest.
  # GPU variant: funasr/server:latest-gpu (set deploy.resources for GPU).
  image: funasr/server:latest
  container_name: audiography-funasr
  profiles: ["real"]
  restart: unless-stopped
  environment:
    FUNASR_MODEL: ${FUNASR_MODEL:-fun-asr-nano}
  ports:
    - "8000:8000"      # OpenAI-compat endpoint (NOT legacy :10095)
  healthcheck:
    test:
      - "CMD-SHELL"
      - "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)\""
    interval: 15s
    timeout: 5s
    retries: 20
    start_period: 180s   # funASR 模型加载较慢
  # Optional GPU — uncomment for fun-asr-large
  # deploy:
  #   resources:
  #     reservations:
  #       devices:
  #         - driver: nvidia
  #           count: all
  #           capabilities: [gpu]
  command: >
    --device ${FUNASR_DEVICE:-cpu}
    --port 8000
    --served-model-name ${FUNASR_MODEL:-fun-asr-nano}
  networks:
    - audiography_net
```

### 7.2 Eval CLI 入口

不增加 compose 服务（CLI 是 one-shot），仅在 `docker-compose run`：

```bash
# 跑评估（mock judge，无 GPU）
docker compose run --rm backend python -m audio_graphy.eval \
  --gold-set examples/eval/smoke.yaml \
  --report-dir reports/ \
  --mock

# 跑评估（real judge，需 --profile real）
docker compose --profile real run --rm backend python -m audio_graphy.eval \
  --gold-set examples/eval/full.yaml \
  --report-dir reports/
```

### 7.3 端口冲突说明

funASR 改用 `8000`（vLLM-strong 也是 8000）。**不冲突**：funASR 仅在 `--profile real` 且 vLLM-strong 同 profile；如同时启用，需将 funASR 改为 `8003:8000`（架构师在 T6 决定）。**默认建议**：funASR `8003:8000`，避免与 vLLM-strong 端口冲突。

---

## 8. 测试策略

### 8.1 WS-1 funASR Adapter（respx）

| 用例 ID | 描述 | HTTP mock | 期望 |
|---------|------|-----------|------|
| `asr_happy_200` | 正常 verbose_json 返回 | 200 + JSON | `ASRResult(text=..., words=...)` |
| `asr_happy_no_segments` | 响应只有 `text` 无 `segments` | 200 + JSON | `ASRResult(text=...)`（segments 兜底为单段） |
| `asr_err_400` | 音频格式错 | 400 | `ASRRequestError` |
| `asr_err_413` | 文件过大 | 413 | `ASRTooLargeError` |
| `asr_err_422` | 不支持的 response_format | 422 | `ASRRequestError` |
| `asr_err_500` | 服务错 | 500 | `ASRServerError` |
| `asr_err_timeout` | 超时 | `httpx.TimeoutException` | `ASRTimeoutError` |

文件：`backend/tests/adapters/real/test_funasr.py`（~220 行，7 用例）

### 8.2 WS-2 Evaluation（单元 + 集成）

| 文件 | 内容 |
|------|------|
| `tests/eval/test_types.py` | GoldExample / PredictedResult / MetricResult dataclass 序列化 |
| `tests/eval/test_metrics_retrieval.py` | Context Precision@k / Recall（含 k=0、空 retrieved、空 gold 边界） |
| `tests/eval/test_metrics_generation.py` | Faithfulness / Answer Relevance / Factual Correctness（mock judge LLM 返回固定 JSON） |
| `tests/eval/test_metrics_audio_graphy.py` | Entity F1 / Edge P×C / Tag Acc（含全角半角、空集合边界） |
| `tests/eval/test_runner.py` | EvalRunner happy path + 1 example 抛错容错 |
| `tests/eval/test_reporter.py` | Markdown + JSON 报告生成（断言关键字段存在） |
| `tests/eval/test_cli.py` | `python -m audio_graphy.eval --mock --gold-set examples/eval/smoke.yaml` end-to-end |

**Mock 策略**：
- LLM-as-judge：在测试中用 `MockLLMAdapter`，prompt 模板不变；mock 返回 JSON 形如 `{"id":1,"supported":true}` 等
- Pipeline（retrieval + generation）：用 `MockRetrievalService` + `MockGenerationService`，断言 metric 公式而非 LLM 输出质量
- 黄金集 fixture：`tests/eval/fixtures/gold_smoke.yaml`（5 条）

### 8.3 测试矩阵（WS-2 共 ~28 用例）

| 模块 | 用例数 |
|------|--------|
| types | 3 |
| metrics_retrieval | 5 |
| metrics_generation | 6 |
| metrics_audio_graphy | 6 |
| runner | 3 |
| reporter | 2 |
| cli | 3 |
| **合计** | **28** |

### 8.4 覆盖率目标

| 模块 | 目标 |
|------|------|
| `adapters/real/funasr.py` | ≥ 90% |
| `adapters/exceptions.py`（新增 ASR 类） | ≥ 95% |
| `eval/metrics/*` | ≥ 95% |
| `eval/runner.py` | ≥ 90% |
| `eval/reporter.py` | ≥ 90% |
| `eval/cli.py` | ≥ 85% |

---

## 9. 验收标准

### 9.1 功能

- [ ] `Settings(adapter_asr_mode="real")` 不 raise（M4 约束解锁）
- [ ] `build_adapters(Settings(adapter_asr_mode="real"))` 返回的 bundle.asr 是 `FunASRAdapter` 实例
- [ ] `pytest backend/tests/ -x` 全绿，总数 ≥ **700**（657 + 7 ASR + 28 eval + 8 杂项）
- [ ] 新模块（`adapters/real/funasr.py` + `eval/`）行覆盖率 ≥ 90%
- [ ] `python -m audio_graphy.eval --gold-set examples/eval/smoke.yaml --mock` 退出码 0，生成 md + json
- [ ] `docker compose --profile real config` YAML 合法
- [ ] `docker compose --profile real up funasr` 容器能起（不要求健康，因无 GPU）

### 9.2 代码质量

- [ ] `ruff check backend/` 0 错
- [ ] `mypy backend/audio_graphy/adapters/real/funasr.py` 0 错
- [ ] `mypy backend/audio_graphy/eval/` 0 错
- [ ] `adapters/real/funasr.py` ≤ 200 行
- [ ] `eval/` 总计 ≤ 1200 行
- [ ] 无硬编码 URL / model name / tenant ID
- [ ] LLM-as-judge prompt 中英双语 docstring（开源透明）

### 9.3 文档

- [ ] `docs/m5-eval.md`：评估指标公式 + gold set 字段说明 + 示例（≥ 200 行）
- [ ] `docs/deployment.md` 更新 funASR 启动步骤（替换 M4 占位段）
- [ ] `.env.example` 覆盖所有 M5 新字段
- [ ] `README.md` 加 M5 状态说明（≤ 10 行）：funASR 解锁 + 评估子系统上线
- [ ] `examples/eval/smoke.yaml` + `examples/eval/README.md`

### 9.4 向后兼容

- [ ] M4 既有 `.env`（`ADAPTER_ASR_MODE=mock`）不改动也能工作
- [ ] M4 API 端点行为不变
- [ ] `FUNASR_URL=http://funasr:10095` 旧值将不再可用（必须改为 `:8000`），需在 `docs/deployment.md` 显式标注 breaking

---

## 10. 待确认问题（≤ 5 个）

### Q1（高）· funASR 镜像 tag 用 `funasr/server:latest` 还是固定 SHA？

`latest` 简单但可能因 funASR 上游 schema 漂移破坏 adapter。**选项**：(a) `latest` / (b) 锁定具体版本 e.g. `funasr/server:1.0.5` / (c) 锁 SHA。**默认 (a)，需确认。**

### Q2（中）· LLM-as-judge 用 strong (qwen3.6-27b) 还是另开一个独立 judge 模型？

复用 strong 节省资源，但同一模型既当被评估方又当裁判有偏差。**选项**：(a) 复用 strong / (b) 配置独立 `JUDGE_LLM_MODEL` 字段，默认 strong。**默认 (a)，需确认。**

### Q3（中）· Entity F1 用什么 tokenizer？

字符级最简但中文实体可能跨字符（如"长安 CS75 Plus"）。**选项**：(a) 无 tokenizer（按字符） / (b) jieba / (c) HanLP。**默认 (a)，需确认。**

### Q4（低）· 评估 CLI 入口是 `python -m audio_graphy.eval` 还是 `audiography-eval`？

`python -m` 无需在 pyproject.toml 注册 entry point；`audiography-eval` 更专业。**默认 (a)，需确认。**

### Q5（低）· 评估指标是否暴露 Prometheus metrics？

暴露后 Grafana 可看趋势，但 M5 不持久化。**选项**：(a) 不暴露（仅落文件） / (b) 暴露 `/metrics` endpoint。**默认 (a)，需确认。**

---

## 附录 · 交付物清单

| 文件 | 状态 | 估算行数 | 工作流 |
|------|------|---------|--------|
| `backend/audio_graphy/adapters/real/funasr.py` | 新增 | 200 | WS-1 |
| `backend/audio_graphy/adapters/exceptions.py` | 改 | +30 | WS-1 |
| `backend/audio_graphy/config.py` | 改 | +15 / -5 | WS-1 |
| `backend/audio_graphy/adapters/bundle.py` | 改 | +10 / -2 | WS-1 |
| `docker-compose.yml` | 改 | +25 / -12 | WS-1 |
| `.env.example` | 改 | +5 / -2 | WS-1 |
| `backend/tests/adapters/real/test_funasr.py` | 新增 | 220 | WS-1 |
| `backend/audio_graphy/eval/__init__.py` | 新增 | 5 | WS-2 |
| `backend/audio_graphy/eval/types.py` | 新增 | 100 | WS-2 |
| `backend/audio_graphy/eval/metrics/__init__.py` | 新增 | 10 | WS-2 |
| `backend/audio_graphy/eval/metrics/retrieval.py` | 新增 | 120 | WS-2 |
| `backend/audio_graphy/eval/metrics/generation.py` | 新增 | 180 | WS-2 |
| `backend/audio_graphy/eval/metrics/audio_graphy.py` | 新增 | 160 | WS-2 |
| `backend/audio_graphy/eval/judge.py` | 新增 | 140 | WS-2 |
| `backend/audio_graphy/eval/prompts/*.txt` | 新增 | 80（3 个 prompt） | WS-2 |
| `backend/audio_graphy/eval/runner.py` | 新增 | 130 | WS-2 |
| `backend/audio_graphy/eval/reporter.py` | 新增 | 150 | WS-2 |
| `backend/audio_graphy/eval/cli.py` | 新增 | 90 | WS-2 |
| `backend/audio_graphy/eval/__main__.py` | 新增 | 5 | WS-2 |
| `backend/tests/eval/*`（7 个测试文件 + fixtures） | 新增 | 750 | WS-2 |
| `examples/eval/smoke.yaml` + `README.md` | 新增 | 150 | WS-2 |
| `docs/m5-eval.md` | 新增 | 250 | WS-2 |
| `docs/deployment.md` | 改 | +30 / -10 | WS-1 |
| `README.md` | 改 | +10 | — |
| `docs/m5-prd.md`（本文件） | 新增 | ≤ 600 | — |
| **总计 M5 增量** | — | **≤ 2900 行** | — |

---

**END OF M5 PRD** — 主理人确认 Q1–Q5 后即可进入架构（高见远）。
