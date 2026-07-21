# AudioGraphy M4 PRD — 真实模型 Adapter（Code-Ready）

| 字段 | 值 |
|------|-----|
| 版本 | v4.0.0-draft |
| 作者 | 许清楚（PM / AI 代行） |
| 主理人 | 齐活林 |
| 日期 | 2026-07-21 |
| 前置 | M3 commit `8f6f841`（623 测试 / 91.54% 覆盖率） |
| 范围 | Code-Ready（只写代码 + compose，不拉起服务） |

---

## 1. 产品目标 + 范围边界

### 1.1 北极星
**让具备 GPU + 模型权重的自部署用户在 30 分钟内从 mock 切换到真实服务，无需改业务层。**

### 1.2 量化目标
- ✅ `build_adapters()` 不再 raise `NotImplementedError`
- ✅ 3 个真实 adapter 满足 `protocols.py` 契约
- ✅ respx 测试覆盖 ≥ 90%（针对新 adapter 模块）
- ✅ `docker compose --profile real config` 通过
- ✅ M3 全部 623 测试 0 回归

### 1.3 In Scope
- Adapter 代码：`vad_silero.py` / `llm_openai.py` / `embed_bge.py`
- ASR 保持 mock（funASR 推迟 M5）
- config 解耦：4 个独立 mode 开关 + 删 `NotImplementedError`
- docker-compose 加 5 个 `profiles: [real]` 服务
- respx mock HTTP 测试
- `docs/deployment.md` + `.env.example`

### 1.4 Out of Scope
- ❌ funASR 真实 adapter / ❌ 实际拉起 GPU 服务 / ❌ 评估 gold set
- ❌ 中文归一化 / ❌ 前端补全 / ❌ PIPL
- ❌ 多 GPU tensor parallel / ❌ 模型下载脚本（README 即可）

---

## 2. 核心用户故事

**US-1 开源集成者**：拿到 GPU 机器后改 4 个 `ADAPTER_*_MODE=real` 即可启用真实模型，无需改业务代码。验收：`docker compose --profile real up` 后 `/health` 全绿。

**US-2 CI 流水线**：无 GPU / 无权重环境下跑全部 adapter 测试 + 验证 HTTP 错误路径。验收：`pytest tests/adapters/real/` 全绿。

**US-3 SRE**：compose 自带 healthcheck + GPU 声明，无需自写。验收：`--profile real ps` 显示 5 服务健康状态。

**US-4 开发者**：强/弱 LLM 复用同一 `LLMOpenAIAdapter` 类，差异仅在 `base_url` + `model`。验收：`bundle.py` strong/weak 实例化对称。

---

## 3. 需求池

### 3.1 P0（blocks release）
| ID | 描述 |
|----|------|
| P0-1 | `adapters/real/vad_silero.py` |
| P0-2 | `adapters/real/llm_openai.py`（强+弱复用） |
| P0-3 | `adapters/real/embed_bge.py` |
| P0-4 | `config.py`：删 `NotImplementedError` + 4 个独立 mode |
| P0-5 | `bundle.py`：新增 `build_hybrid_bundle()` |
| P0-6 | docker-compose：5 个 `profiles: [real]` 服务 |
| P0-7 | respx 测试：3 adapter × (happy + timeout + 5xx + 4xx + JSON 解析失败) |
| P0-8 | `docs/deployment.md` |
| P0-9 | `.env.example` |

### 3.2 P1（可推迟 M5）
- Adapter metrics（Prometheus）
- LLM 重试 + 指数退避（tenacity）
- Embedding 批处理（>64 自动分批）

---

## 4. 三个 Adapter HTTP 契约

> **以下契约为 M4 实现的 source of truth。**

### 4.1 VAD — Silero VAD
**端点**：`POST {SILERO_VAD_URL}/v1/vad/segment`

**请求**（multipart/form-data）：
| Field | Type | Required | Default |
|-------|------|----------|---------|
| `audio` | file (wav) | ✅ | — |
| `min_segment_sec` | float | ❌ | 0.5 |
| `max_segment_sec` | float | ❌ | 30.0 |

**响应 200**：
```json
{"segments": [{"start_sec": 0.0, "end_sec": 5.32, "confidence": 0.95}], "model": "silero-vad-v5"}
```

**状态码**：200 OK / 400 音频格式（`VADRequestError`）/ 413 文件过大 / 500 服务（`VADServerError`）/ 504 超时（`VADTimeoutError`）。

```bash
curl -X POST http://silero-vad:8002/v1/vad/segment \
  -F "audio=@test.wav" -F "min_segment_sec=0.5"
```

### 4.2 LLM — vLLM OpenAI 兼容
**端点**：`POST {base_url}/chat/completions`（标准 OpenAI schema）

**两档实例**：
| 角色 | base_url | model | 用途 |
|------|----------|-------|------|
| Strong | `http://vllm-strong:8000/v1` | `qwen3.6-27b` | 实体抽取 / 终答 / segment filter |
| Weak | `http://vllm-weak:8001/v1` | `qwen3.6-35b-a3b` | 查询改写 / 摘要 / 关键词 |

**状态码**：200 / 400 `LLMBadRequest` / 429 `LLMRateLimit`（M4 不重试）/ 5xx `LLMServerError` / 网络超时 `LLMTimeoutError`（httpx 默认 60s）。

```bash
curl -X POST http://vllm-strong:8000/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen3.6-27b","messages":[{"role":"user","content":"hi"}]}'
```

### 4.3 Embedding — bge-m3
**端点**：`POST {BGE_M3_URL}/v1/embeddings`（OpenAI 兼容）

**请求**：`{"input": ["text1", "text2"], "model": "bge-m3"}`

**响应 200**：`{"model":"bge-m3","data":[{"index":0,"embedding":[...1024 维]}]}`

**约束**：dim 固定 1024 / 单次 ≤ 64 条（>64 在 adapter 分批，P1）/ >512 tokens 服务端截断不报错 / dim mismatch → `EmbedDimMismatchError`。

```bash
curl -X POST http://bge-m3:8080/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input":["hello"],"model":"bge-m3"}'
```

---

## 5. docker-compose real Profile 设计

新增 5 个服务全部 `profiles: ["real"]`，仅 `--profile real up` 才启动：

| 服务 | image | 端口 | GPU | start_period |
|------|-------|------|-----|--------------|
| `vllm-strong` | `vllm/vllm-openai:latest` | 8000:8000 | ✅ 必需 | 300s |
| `vllm-weak` | `vllm/vllm-openai:latest` | 8001:8000 | ✅ 必需 | 300s |
| `silero-vad` | `jetresearch/silero-vad-server:latest` | 8002:8000 | 可选 | 30s |
| `bge-m3` | `ghcr.io/huggingface/text-embeddings-inference:1.5` | 8080:80 | ✅ 必需 | 120s |
| `funasr` | `registry.cn-hangzhou.aliyuncs.com/funasr_recog/funasr-runtime-sdk-online-cpu-0.1.12` | 10095:10095 | 无 | — |

**关键配置**：
- vLLM 服务通过 `--served-model-name` 对齐 `config.llm_strong_model` / `llm_weak_model`
- 健康检查统一用 `python -c "urllib.request.urlopen('/health')"`（容器无 curl）
- vLLM / bge-m3 用 `deploy.resources.reservations.devices` 声明 GPU
- 新增 volumes：`vllm_cache` / `tei_cache`
- funASR 不写 healthcheck（M5 实装 ASR 时再加）

**启动姿势**：
```bash
docker compose up -d                    # mock 模式（M1-M3 行为）
docker compose --profile real up -d     # 全 real
docker compose --profile real up bge-m3 backend mysql  # 混搭
```

---

## 6. config.py 变更

### 6.1 删除
```python
# config.py L184 删掉
raise NotImplementedError("ADAPTER_MODE=real ...")
```

### 6.2 新增字段
```python
adapter_asr_mode: AdapterMode = "mock"     # M4 强制 mock
adapter_vad_mode: AdapterMode = "mock"
adapter_llm_mode: AdapterMode = "mock"
adapter_embed_mode: AdapterMode = "mock"
# 旧 adapter_mode 保留作为"全局默认"——per-adapter 未显式设置则继承
```

### 6.3 validator 改动
```python
@model_validator(mode="after")
def _validate_combinations(self) -> Settings:
    # 既有
    if not 0 <= self.mock_llm_error_rate <= 1:
        raise ValueError("MOCK_LLM_ERROR_RATE must be in [0.0, 1.0]")
    # M4 新增
    if self.adapter_asr_mode == "real":
        raise ValueError("ADAPTER_ASR_MODE=real not supported in M4 (funASR lands in M5)")
    # M4 新增：任一 real → 提醒 jwt
    real_modes = [self.adapter_vad_mode, self.adapter_llm_mode, self.adapter_embed_mode]
    if "real" in real_modes and self.jwt_secret.startswith("change-me"):
        logger.warning("REAL adapter ON but JWT_SECRET is placeholder")
    return self
```

### 6.4 `build_adapters()` 重构
```python
def build_adapters(settings: Settings) -> AdapterBundle:
    from audio_graphy.adapters.bundle import build_mock_bundle, build_hybrid_bundle
    all_mock = all(m == "mock" for m in [
        settings.adapter_asr_mode, settings.adapter_vad_mode,
        settings.adapter_llm_mode, settings.adapter_embed_mode,
    ])
    if all_mock:
        return build_mock_bundle(settings)
    return build_hybrid_bundle(settings)
```

### 6.5 `bundle.py` 新增 `build_hybrid_bundle()`
- ASR 强制 `MockASRAdapter`（M5 解锁）
- VAD：`real` → `SileroVADAdapter(url=settings.silero_vad_url)` / 否则 `MockVADAdapter()`
- LLM：`real` → 实例化两个 `LLOpenAIAdapter`（strong + weak，参数从 settings 读）
- Embed：`real` → `BGEmbedAdapter(url=settings.bge_m3_url)` / 否则 `MockEmbedAdapter()`

### 6.6 `.env.example` 新增片段
```dotenv
ADAPTER_MODE=mock                   # global default
ADAPTER_ASR_MODE=mock               # M4: must be mock
ADAPTER_VAD_MODE=mock               # set to real for Silero
ADAPTER_LLM_MODE=mock               # set to real for vLLM
ADAPTER_EMBED_MODE=mock             # set to real for bge-m3
SILERO_VAD_URL=http://silero-vad:8002
BGE_M3_URL=http://bge-m3:8080
OPENAI_BASE_URL_STRONG=http://vllm-strong:8000/v1
OPENAI_BASE_URL_WEAK=http://vllm-weak:8001/v1
OPENAI_API_KEY=dummy
LLM_STRONG_MODEL=qwen3.6-27b
LLM_WEAK_MODEL=qwen3.6-35b-a3b
EMBEDDING_DIM=1024
HF_TOKEN=                           # required for gated Qwen models
```

---

## 7. 集成测试策略

### 7.1 工具栈
`respx` 拦截 httpx（M3 已有依赖）+ `pytest-asyncio`。**不引入新依赖。**

### 7.2 测试矩阵（共 18 用例）

| Adapter | 用例 |
|---------|------|
| `vad_silero.py` | happy_200 / err_400_bad_audio / err_413_too_large / err_500_server / err_timeout / err_bad_json |
| `llm_openai.py` | happy_strong / happy_weak / happy_with_cache（第二次 `cached=True`）/ err_400_bad_messages / err_429_rate_limit / err_500_server / err_timeout |
| `embed_bge.py` | happy_single / happy_batch / err_500 / err_timeout / err_dim_mismatch |

### 7.3 文件布局
```
backend/tests/adapters/real/
├── conftest.py             # 临时 wav fixture / settings overrides
├── test_vad_silero.py
├── test_llm_openai.py
└── test_embed_bge.py
```

### 7.4 不做
- ❌ docker-compose 集成测试（无 GPU）
- ❌ load test / 真实模型输出质量评估

---

## 8. 验收标准

### 8.1 功能
- [ ] 任意 mode 组合启动 backend 不 raise（除 `adapter_asr_mode=real`）
- [ ] `pytest backend/tests/ -x` 全绿，总数 ≥ **641**（623 旧 + 18 新）
- [ ] 新 adapter 模块行覆盖率 ≥ 90%
- [ ] `docker compose --profile real config` YAML 合法
- [ ] `docker compose --profile real up funasr` 容器能起（不要求健康）

### 8.2 代码质量
- [ ] `ruff check backend/` 0 错
- [ ] `mypy backend/audio_graphy/adapters/real/` 0 错
- [ ] 3 个新 adapter 文件 ≤ 200 行 each
- [ ] 无硬编码 URL / model name

### 8.3 文档
- [ ] `docs/deployment.md`：硬件需求表 / 启动步骤 / 故障排查 FAQ / 模型下载指引
- [ ] `.env.example` 覆盖所有 M4 新字段
- [ ] `README.md` 加 M4 状态说明（≤ 10 行）

### 8.4 向后兼容
- [ ] M3 既有 `.env` 不改动也能工作（全 mock 默认值不变）
- [ ] M3 API 端点行为不变

---

## 9. 待确认问题（≤ 5 个）

### Q1（高）· `jetresearch/silero-vad-server` 镜像是否可信？
非 Silero 官方，社区维护。**选项**：(a) 接受写进 compose / (b) 自己 fork / (c) VAD 也用 mock 推迟 M4.5。**默认 (a)，需确认。**

### Q2（高）· vLLM 镜像 tag 用 `latest` 还是锁版本？
`latest` 可能因版本漂移破坏 OpenAI schema。**选项**：(a) `latest` / (b) 锁 `v0.7.2` / (c) 锁 SHA。**默认 (b)，需确认。**

### Q3（中）· 强 LLM 单卡硬件门槛
27B 至少 1× A100 80G 或 2× 4090。**默认**：在 deployment.md 写明。**已自洽，无 Action。**

### Q4（中）· bge-m3 端口对齐
TEI 内部 listen 80，docker `8080:80` 映射，`BGE_M3_URL=http://bge-m3:8080` 不含 `/v1`（adapter 内部补）。**已自洽，无 Action。**

### Q5（低）· 旧 `adapter_mode` 字段是否保留？
M4 引入 4 个独立字段后旧字段语义变模糊，但删除会破坏 M3 用户 `.env`。**选项**：(a) 保留作"全局默认" / (b) 保留 + validator 显式优先级 / (c) 删除强制 4 字段。**默认 (b)，需确认。**

---

## 附录 · 交付物清单

| 文件 | 状态 | 估算行数 |
|------|------|---------|
| `backend/audio_graphy/adapters/real/__init__.py` | 新增 | 5 |
| `backend/audio_graphy/adapters/real/vad_silero.py` | 新增 | 180 |
| `backend/audio_graphy/adapters/real/llm_openai.py` | 新增 | 200 |
| `backend/audio_graphy/adapters/real/embed_bge.py` | 新增 | 150 |
| `backend/audio_graphy/adapters/exceptions.py` | 新增 | 60 |
| `backend/audio_graphy/config.py` | 改 | +30 / -10 |
| `backend/audio_graphy/adapters/bundle.py` | 改 | +50 |
| `docker-compose.yml` | 改 | +90 |
| `.env.example` | 新增 | 80 |
| `docs/deployment.md` | 新增 | 250 |
| `backend/tests/adapters/real/*` | 新增 | 600 |
| `docs/m4-prd.md`（本文件） | 新增 | ≤ 500 |
| **总计** | — | **≤ 2200 行** |

---

**END OF M4 PRD** — 主理人确认 Q1 / Q2 / Q5 后即可进入实施。
