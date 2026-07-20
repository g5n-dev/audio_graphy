# AudioGraphy M3 PRD — 认证中间件 + 标签治理 + 业务路由 + 集成测试

> **里程碑**: M3 · 多租户 HTTP 服务层（合并原 M3+M4+M5 关键路径）
> **版本**: v1.0 · 2026-07
> **作者**: 许清楚 (Xu) · 产品经理
> **权威源**: `docs/DESIGN.md` §6, §12, §14
> **前置里程碑**: M1（基础设施 · 190 测试全过）+ M2（算法内核 · 394 测试 · 92.14% 覆盖率）· Phase 1 go/no-go 关卡已过

---

## 1. 产品目标 (Product Goals)

M3 的目标是把 M2 已验证的算法内核**暴露为可用的多租户 HTTP 服务**——让外部系统（前端 / 第三方 / CI 流水线）能通过 RESTful API 完成录音入库、图谱检索、标签打标与重算、Prompt 版本管理等完整业务闭环，同时保证认证授权、租户隔离、审计可追溯。

| # | 目标 (Goal) | 衡量标准 (Measurable Criteria) |
|---|---|---|
| G1 | **认证授权可用**：JWT 签发/校验 + 4 角色 RBAC + 多租户行级隔离，跨租户访问不暴露资源存在性 | 全 RBAC 矩阵覆盖测试通过；跨租户访问统一返回 404 |
| G2 | **标签治理闭环**：tag_facts → tag_current → tag_stats 三层流转 + Prompt 版本切换触发 delta 重算 + LLM 缓存幂等 | 重算只对 dirty 数据增量更新；同 prompt 重打命中缓存 0 token |
| G3 | **业务路由完整**：9 个 router 覆盖录音生命周期 + 检索 + 图谱 + 标签 + Prompt + 健康，全链路端到端可跑 | 端到端集成测试：upload → index → query → tag → recompute 全过 |

### 1.1 M3 范围边界 (Scope Boundary)

| ✅ In Scope（M3 做） | ❌ Out of Scope（M3 不做） | 何时做 |
|---|---|---|
| JWT 签发/校验 + refresh token | OAuth2 / SSO / 第三方登录 | Phase 4 |
| 4 角色 RBAC 中间件（admin/inspector/agent/viewer） | 细粒度 ACL / 自定义角色 | — |
| 多租户行级隔离（tenant_id 强制注入） | 物理库隔离 / schema-per-tenant | — |
| 录音上传（文件 path 注册，非 multipart 流式上传） | 大文件分片上传 / 断点续传 | Phase 4 |
| 后台 pipeline worker（APScheduler，拉取 queued → indexed） | 实时流式索引 | Phase 4 |
| tag 三层 delta 重算（含 LLM 缓存幂等） | 全量批跑 / 分类法变更重算 | — |
| Prompt CRUD + activate + diff | Prompt A/B 测试框架 / 自动评估 | Phase 4（评估） |
| 图谱节点/边/子图查询（只读 API） | 图谱在线编辑 / 增删节点 | — |
| 健康检查（DB + adapters 可达性） | Prometheus 指标 / Grafana 面板 | Phase 4（运维） |
| Arco 前端页面 | — | 独立里程碑 |
| 评估任务 API（/eval/*） | — | Phase 4 |
| 录音文件 AES-256 加密 / PIPL 合规 | — | Phase 4 |
| 说话人节点 / CLAP 音频嵌入 | — | Phase 2 |

---

## 2. 用户故事 (User Stories)

| # | 角色 (Role) | 故事 (Story) |
|---|---|---|
| US1 | **管理员 (Admin)** | 作为租户管理员，我希望通过 API 上传录音、管理用户角色、切换 Prompt 版本并触发重算，这样我能管控整个质检流程。 |
| US2 | **质检员 (Inspector)** | 作为质检员，我希望对录音列表做自然语言问答（带时间窗过滤）、浏览知识图谱、人工修正标签，并能溯源到具体录音时间段核实。 |
| US3 | **坐席 (Agent)** | 作为门店坐席，我希望只能看到自己的录音和标签统计，不能看到其他坐席的数据，也不能修改标签或管理 Prompt。 |
| US4 | **只读用户 (Viewer)** | 作为只读用户（如区域经理），我希望查看本租户的标签统计看板和图谱，但不能做任何写操作。 |
| US5 | **开源集成者 (OSS Integrator)** | 作为想把 AudioGraphy 嵌入自己系统的开发者，我希望 API 有清晰的 OpenAPI 文档、统一的错误格式、可 mock 的 adapter，这样我能快速对接。 |
| US6 | **安全审计员 (Security Auditor)** | 作为安全审计员，我希望跨租户访问返回 404（而非 403），所有写操作有审计日志，JWT 有过期和刷新机制。 |

---

## 3. 需求池 (Requirements Pool)

### 3.1 认证中间件 (Auth)

| ID | 优先级 | 描述 |
|---|---|---|
| AUTH-01 | **P0** | JWT 签发（`jwt_utils.py`）：HS256 签名，payload 含 `sub`(user_id) / `tid`(tenant_id) / `role` / `exp`，过期时间 `jwt_exp_hours`（默认 12h） |
| AUTH-02 | **P0** | JWT 校验中间件（`middleware.py`）：从 `Authorization: Bearer <token>` 提取并校验，失败返回 401 |
| AUTH-03 | **P0** | 多租户行级隔离（`middleware.py`）：从 JWT 提取 `tid`，注入到 request.state，所有 DB 查询强制 `WHERE tenant_id = ?` |
| AUTH-04 | **P0** | RBAC 角色守卫（`roles.py`）：4 角色 admin/inspector/agent/viewer，端点级 `Depends(require_role("admin"))` 装饰 |
| AUTH-05 | **P0** | 跨租户隔离（`tenants.py`）：查询返回不属于本租户的资源时返回 **404 Not Found**（不暴露存在性） |
| AUTH-06 | **P0** | Agent 角色数据范围限制：Agent 只能查看 `agent_name == self.name` 的录音（列表/详情/标签） |
| AUTH-07 | **P1** | Refresh token 端点：用有效 refresh_token 换新 access_token |
| AUTH-08 | **P1** | `/auth/me` 端点：返回当前用户信息（id / name / email / role / tenant_id） |
| AUTH-09 | **P2** | JWT 黑名单（logout 即时失效）— P2 可选，默认靠过期 |

### 3.2 标签治理 (Tags)

| ID | 优先级 | 描述 |
|---|---|---|
| TAG-01 | **P0** | tag_facts 写入（`facts.py`）：append-only，每次打标 INSERT 新 version 行，带完整配方（prompt_version / model_version / input_hash / confidence） |
| TAG-02 | **P0** | tag_current 刷新（`current_view.py`）：每次 tag_facts INSERT 后 upsert tag_current = MAX(version) |
| TAG-03 | **P0** | tag_stats delta 聚合（`stats.py`）：增量刷新 `-old +new`，维度 `(tenant_id, store_id, agent_name, tag_path, tag_value)` |
| TAG-04 | **P0** | Prompt 版本切换触发 delta 重算（`recompute.py`）：激活新 prompt_version → 查 `prompt_version < new` 的 recording → 重打 → diff → 只 commit 变化 → 增量聚合 |
| TAG-05 | **P0** | LLM 缓存幂等：同 prompt 重打命中 `kv_store_llm_response_cache.json`，0 token |
| TAG-06 | **P1** | 人工修正标签：inspector/admin 手动写入 tag_fact（source=manual），触发 tag_current + tag_stats 刷新 |
| TAG-07 | **P1** | 重算任务状态查询：返回 `pending / running / done / failed` + affected_count |
| TAG-08 | **P2** | APScheduler 后台 worker 定时扫描 queued 录音并执行 pipeline |

### 3.3 业务路由 (API Routers)

| ID | 优先级 | Router | 描述 |
|---|---|---|---|
| API-01 | **P0** | `api/auth.py` | POST /login · POST /refresh · GET /me |
| API-02 | **P0** | `api/recordings.py` | POST /recordings · GET /recordings · GET /recordings/{id} · GET /recordings/{id}/status |
| API-03 | **P0** | `api/segments.py` | GET /recordings/{id}/segments |
| API-04 | **P0** | `api/query.py` | POST /query（双通道检索 + 时间过滤 + 回答生成） |
| API-05 | **P0** | `api/graph.py` | GET /graph/explore · GET /graph/entity/{name} · GET /graph/subgraph |
| API-06 | **P0** | `api/tags.py` | GET /recordings/{id}/tags · POST /recordings/{id}/tags · POST /tags/recompute |
| API-07 | **P0** | `api/prompts.py` | GET /prompts · POST /prompts · GET /prompts/{id} · POST /prompts/{id}/activate |
| API-08 | **P0** | `api/stats.py` | GET /tags/stats（多维聚合下钻） |
| API-09 | **P0** | `api/health.py` | GET /health/readiness（DB + adapters 可达性） |
| API-10 | **P1** | `api/recordings.py` | POST /recordings/{id}/reindex（重新索引） |
| API-11 | **P1** | `api/graph.py` | GET /graph/path（两实体间最短路径） |
| API-12 | **P2** | `api/admin.py` | 租户/用户管理 CRUD（admin only） |

### 3.4 集成测试

| ID | 优先级 | 描述 |
|---|---|---|
| TEST-01 | **P0** | `test_auth_flow.py`：login → 拿 token → 访问 /me → 跨租户 404 → 过期 token 401 → 角色越权 403 |
| TEST-02 | **P0** | `test_ingest_pipeline.py`：upload → status 轮询 → indexed → segments 可查 → tags 可查 |
| TEST-03 | **P0** | `test_query_retrieve.py`：query → answer + citations 非空 → 时间过滤生效 → 双通道命中 |
| TEST-04 | **P0** | `test_tags_recompute.py`：打标 → activate 新 prompt → recompute → tag_current 更新 → tag_stats delta 正确 → 缓存命中 |
| TEST-05 | **P0** | RBAC 矩阵全覆盖：4 角色 × 关键端点（≥ 20 组合） |
| TEST-06 | **P1** | OpenAPI schema 完整性：`/openapi.json` 可生成，所有端点有描述 + schema |

### 3.5 通用质量

| ID | 优先级 | 描述 |
|---|---|---|
| QUAL-01 | **P0** | 统一错误响应格式：`{ "error": { "code": "...", "message": "...", "detail": {...} } }`，HTTP 状态码 400/401/403/404/409/422/500 |
| QUAL-02 | **P0** | 全部端点加 docstring + Pydantic schema（请求/响应模型） |
| QUAL-03 | **P0** | `ruff check && ruff format --check` 全过 + mypy strict 全过 |
| QUAL-04 | **P1** | 结构化日志（JSON）：含 `request_id / tenant_id / user_id / endpoint / latency_ms` |
| QUAL-05 | **P1** | request_id 中间件：每个请求注入 UUID，贯穿日志 + 响应头 `X-Request-ID` |

---

## 4. API 契约 (API Contract)

> **约定**：
> - 所有路径前缀 `/api/v1`
> - 除 `/auth/login` 外所有端点需 `Authorization: Bearer <token>`
> - 请求/响应 Content-Type: `application/json`
> - 时间格式：ISO 8601 UTC（`2026-07-15T08:30:00Z`）
> - 分页参数：`page`（从 1 开始）+ `page_size`（默认 20，最大 100）
> - "角色要求"列标注该端点允许的最小角色集

### 4.1 认证 (Auth)

---

#### POST /api/v1/auth/login

用户登录，获取 JWT access token + refresh token。

| 属性 | 值 |
|---|---|
| **认证** | 无（公开端点） |
| **角色要求** | 无 |

**请求 Body**:
```json
{
  "email": "admin@changantest.com",
  "password": "secure-password"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `email` | string | ✅ | email 格式 | 用户邮箱 |
| `password` | string | ✅ | 非空 | 明文密码（TLS 传输） |

**响应 200 OK**:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 43200,
  "user": {
    "id": 1,
    "name": "张管理",
    "email": "admin@changantest.com",
    "role": "admin",
    "tenant_id": "chang_an"
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `access_token` | string | JWT access token |
| `refresh_token` | string | JWT refresh token（有效期 = access × 7） |
| `token_type` | string | 固定 `"bearer"` |
| `expires_in` | int | access_token 有效期（秒） |
| `user.id` | int | 用户 ID |
| `user.name` | string | 用户名 |
| `user.email` | string | 邮箱 |
| `user.role` | string | 角色：admin/inspector/agent/viewer |
| `user.tenant_id` | string | 租户 ID |

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 401 | `INVALID_CREDENTIALS` | 邮箱或密码错误 |
| 403 | `TENANT_DISABLED` | 租户已禁用（P2） |

---

#### POST /api/v1/auth/refresh

用 refresh_token 换新 access_token。

| 属性 | 值 |
|---|---|
| **认证** | 无（用 refresh_token） |
| **角色要求** | 无 |

**请求 Body**:
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**响应 200 OK**:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 43200
}
```

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 401 | `INVALID_REFRESH_TOKEN` | refresh_token 无效或已过期 |

---

#### GET /api/v1/auth/me

获取当前登录用户信息。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | 任意已认证用户 |

**响应 200 OK**:
```json
{
  "id": 1,
  "name": "张管理",
  "email": "admin@changantest.com",
  "role": "admin",
  "tenant_id": "chang_an",
  "created_at": "2026-07-01T00:00:00Z"
}
```

---

### 4.2 录音 (Recordings)

---

#### POST /api/v1/recordings

注册新录音（注册文件路径，触发后台 pipeline）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**请求 Body**:
```json
{
  "store_id": "STORE_001",
  "path": "/data/audio/2026-07-15/rec_001.wav",
  "agent_name": "李坐席",
  "customer_hash": "a1b2c3d4e5f6...",
  "recorded_at": "2026-07-15T10:30:00Z",
  "prompt_version": "tag_prompt_v1"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `store_id` | string | ✅ | ≤ 64 字符 | 门店 ID |
| `path` | string | ✅ | ≤ 512 字符，文件须存在 | 录音文件路径 |
| `agent_name` | string | ❌ | ≤ 255 字符 | 坐席姓名 |
| `customer_hash` | string | ❌ | ≤ 64 字符，SHA-256 | 客户标识哈希（PIPL 合规） |
| `recorded_at` | datetime | ❌ | ISO 8601 | 录制时间（为空则用文件 mtime） |
| `prompt_version` | string | ❌ | ≤ 64 字符 | 指定打标 prompt 版本（为空用 active 版本） |

**响应 201 Created**:
```json
{
  "id": 1001,
  "tenant_id": "chang_an",
  "store_id": "STORE_001",
  "path": "/data/audio/2026-07-15/rec_001.wav",
  "agent_name": "李坐席",
  "customer_hash": "a1b2c3d4e5f6...",
  "recorded_at": "2026-07-15T10:30:00Z",
  "status": "queued",
  "pipeline_state": "pending",
  "prompt_version": "tag_prompt_v1",
  "indexed_at": null,
  "created_at": "2026-07-15T10:31:00Z"
}
```

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 400 | `FILE_NOT_FOUND` | path 指向的文件不存在 |
| 409 | `DUPLICATE_RECORDING` | 同租户同 path 已注册 |
| 422 | `VALIDATION_ERROR` | 字段校验失败 |

---

#### GET /api/v1/recordings

录音列表（含筛选）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / viewer（全量）；agent（仅自己） |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `page` | int | ❌ | 1 | 页码（从 1 开始） |
| `page_size` | int | ❌ | 20 | 每页条数（max 100） |
| `store_id` | string | ❌ | — | 按门店筛选 |
| `status` | string | ❌ | — | queued/processing/indexed/failed/archived |
| `agent_name` | string | ❌ | — | 按坐席筛选（agent 角色强制 = 自己） |
| `recorded_from` | datetime | ❌ | — | 录制时间下界（ISO 8601） |
| `recorded_to` | datetime | ❌ | — | 录制时间上界（ISO 8601） |
| `sort` | string | ❌ | `-recorded_at` | 排序字段（`recorded_at` / `-recorded_at` / `created_at` / `-created_at`） |

**响应 200 OK**:
```json
{
  "items": [
    {
      "id": 1001,
      "store_id": "STORE_001",
      "agent_name": "李坐席",
      "status": "indexed",
      "pipeline_state": "done",
      "recorded_at": "2026-07-15T10:30:00Z",
      "indexed_at": "2026-07-15T10:45:00Z",
      "prompt_version": "tag_prompt_v1"
    }
  ],
  "total": 156,
  "page": 1,
  "page_size": 20
}
```

---

#### GET /api/v1/recordings/{id}

录音详情（含 segments 概要 + current tags）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / viewer（全量）；agent（仅自己） |

**路径参数**:

| 参数 | 类型 | 说明 |
|---|---|---|
| `id` | int | 录音 ID |

**响应 200 OK**:
```json
{
  "id": 1001,
  "tenant_id": "chang_an",
  "store_id": "STORE_001",
  "agent_name": "李坐席",
  "customer_hash": "a1b2c3d4...",
  "path": "/data/audio/2026-07-15/rec_001.wav",
  "status": "indexed",
  "pipeline_state": "done",
  "recorded_at": "2026-07-15T10:30:00Z",
  "prompt_version": "tag_prompt_v1",
  "indexed_at": "2026-07-15T10:45:00Z",
  "created_at": "2026-07-15T10:31:00Z",
  "segments_count": 12,
  "chunks_count": 3,
  "current_tags": [
    {
      "tag_path": "quality.greeting",
      "tag_value": "pass",
      "version": 2,
      "prompt_version": "tag_prompt_v1"
    }
  ]
}
```

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 404 | `RECORDING_NOT_FOUND` | ID 不存在**或跨租户** |

---

#### GET /api/v1/recordings/{id}/status

录音处理状态（轻量查询，不含 segments/tags）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent（仅自己）/ viewer |

**响应 200 OK**:
```json
{
  "id": 1001,
  "status": "indexed",
  "pipeline_state": "done",
  "indexed_at": "2026-07-15T10:45:00Z"
}
```

---

#### POST /api/v1/recordings/{id}/reindex *(P1)*

触发重新索引（重跑 pipeline）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**请求 Body**: 空或 `{ "force": false }`

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `force` | bool | ❌ | false | true = 忽略 content_hash 幂等，强制重跑 |

**响应 200 OK**:
```json
{
  "id": 1001,
  "status": "queued",
  "pipeline_state": "pending",
  "message": "Reindex triggered"
}
```

---

### 4.3 分段 (Segments)

---

#### GET /api/v1/recordings/{id}/segments

录音的 VAD 分段列表。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / viewer（全量）；agent（仅自己） |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `page` | int | ❌ | 1 | — |
| `page_size` | int | ❌ | 50 | — |

**响应 200 OK**:
```json
{
  "recording_id": 1001,
  "items": [
    {
      "id": 5001,
      "idx": 0,
      "start_sec": 0.0,
      "end_sec": 15.32,
      "transcript": "您好，欢迎来到长安汽车...",
      "speaker": null,
      "vad_conf": 0.98
    }
  ],
  "total": 12,
  "page": 1,
  "page_size": 50
}
```

**错误响应**: `404 RECORDING_NOT_FOUND`

---

### 4.4 检索问答 (Query)

---

#### POST /api/v1/query

自然语言问答——双通道检索 + LLM 过滤精化 + 带三级溯源的回答生成。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent / viewer |

**请求 Body**:
```json
{
  "query": "7月哪些接待提到了CS75 Plus的金融政策？",
  "time_range": {
    "start": "2026-07-01T00:00:00Z",
    "end": "2026-07-31T23:59:59Z"
  },
  "top_k": 10,
  "store_id": "STORE_001"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `query` | string | ✅ | 1-500 字符 | 自然语言问题 |
| `time_range` | object | ❌ | — | 时间窗过滤 |
| `time_range.start` | datetime | ✅* | ISO 8601 | 时间下界 |
| `time_range.end` | datetime | ✅* | ISO 8601 | 时间上界 |
| `top_k` | int | ❌ | 1-50，默认 10 | 召回候选数 |
| `store_id` | string | ❌ | ≤ 64 字符 | 限定门店范围 |

> *time_range 若提供则 start + end 均必填。

**响应 200 OK**:
```json
{
  "query": "7月哪些接待提到了CS75 Plus的金融政策？",
  "answer": "在7月1日至15日期间，共3条录音提到了CS75 Plus的金融政策。其中STORE_001的李坐席在7月3日的接待中明确推荐了\"24期0利息\"方案...",
  "citations": [
    {
      "entity": "CS75 Plus",
      "chunk_id": 2001,
      "segment_ids": [5001, 5002],
      "recording_id": 1001,
      "recorded_at": "2026-07-03T14:20:00Z",
      "transcript_snippet": "这款CS75 Plus现在有24期0利息的金融政策...",
      "confidence": "EXTRACTED"
    }
  ],
  "retrieval_stats": {
    "naive_hits": 8,
    "graph_hits": 5,
    "filtered_by_time": 3,
    "filtered_by_judge": 2
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 原始问题 |
| `answer` | string | LLM 生成的回答 |
| `citations` | array | 三级溯源引用列表 |
| `citations[].entity` | string | 命中实体名 |
| `citations[].chunk_id` | int | 溯源 1：entity → chunk |
| `citations[].segment_ids` | int[] | 溯源 2：chunk → segments |
| `citations[].recording_id` | int | 溯源 3：segment → recording |
| `citations[].recorded_at` | datetime | 录制时间 |
| `citations[].transcript_snippet` | string | 段级原文摘要 |
| `citations[].confidence` | string | 边置信度：EXTRACTED/INFERRED/AMBIGUOUS |
| `retrieval_stats.naive_hits` | int | naive 通道命中数 |
| `retrieval_stats.graph_hits` | int | 图谱通道命中数 |
| `retrieval_stats.filtered_by_time` | int | 时间过滤掉的候选数 |
| `retrieval_stats.filtered_by_judge` | int | LLM as-judge 过滤掉的段数 |

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 422 | `VALIDATION_ERROR` | query 为空或超长 |

---

### 4.5 知识图谱 (Graph)

---

#### GET /api/v1/graph/explore

图谱浏览——返回全图节点/边（分页或子图）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent / viewer |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `node_type` | string | ❌ | — | 按实体类型过滤：客户/坐席/车型/价格方案/金融政策/优惠权益/竞品/预约事件 |
| `min_degree` | int | ❌ | 0 | 最小连接数（过滤 god node） |
| `limit` | int | ❌ | 200 | 返回节点上限（max 2000） |

**响应 200 OK**:
```json
{
  "nodes": [
    {
      "id": "CS75_Plus",
      "label": "CS75 Plus",
      "type": "车型",
      "degree": 15,
      "source_ids": ["1001_2001", "1002_2010"],
      "recording_ids": [1001, 1002],
      "recorded_at_range": ["2026-07-03T14:20:00Z", "2026-07-10T09:15:00Z"]
    }
  ],
  "edges": [
    {
      "source": "CS75_Plus",
      "target": "24期0利息",
      "relation": "推荐",
      "weight": 3.0,
      "confidence": "EXTRACTED",
      "confidence_score": 1.0
    }
  ],
  "total_nodes": 45,
  "total_edges": 120
}
```

---

#### GET /api/v1/graph/entity/{name}

实体详情（属性 + 1-hop 邻居 + source_ids）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent / viewer |

**路径参数**:

| 参数 | 类型 | 说明 |
|---|---|---|
| `name` | string | 实体名（URL-encoded） |

**响应 200 OK**:
```json
{
  "node": {
    "id": "CS75_Plus",
    "label": "CS75 Plus",
    "type": "车型",
    "description": "长安汽车旗下紧凑型SUV，客户关注度最高车型之一",
    "degree": 15,
    "source_ids": ["1001_2001", "1002_2010"],
    "recording_ids": [1001, 1002],
    "recorded_at_range": ["2026-07-03T14:20:00Z", "2026-07-10T09:15:00Z"]
  },
  "neighbors": [
    {
      "id": "24期0利息",
      "label": "24期0利息",
      "type": "金融政策",
      "relation": "推荐",
      "weight": 3.0,
      "confidence": "EXTRACTED"
    }
  ],
  "relation_counts": {
    "推荐": 5,
    "询问": 3,
    "对比": 2
  }
}
```

**错误响应**: `404 ENTITY_NOT_FOUND`

---

#### GET /api/v1/graph/subgraph

以指定实体为中心提取 N-hop 子图。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent / viewer |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `entity` | string | ✅ | — | 中心实体名 |
| `max_hops` | int | ❌ | 1 | 扩展跳数（1-3） |
| `limit` | int | ❌ | 50 | 返回节点上限 |

**响应 200 OK**: 结构同 `/graph/explore`。

---

#### GET /api/v1/graph/path *(P1)*

两实体间最短路径。

**Query 参数**: `source` (string, ✅) · `target` (string, ✅)

**响应 200 OK**:
```json
{
  "path": ["CS75_Plus", "24期0利息", "金融方案A"],
  "length": 2,
  "edges": [ { ... }, { ... } ]
}
```

**错误响应**: `404 PATH_NOT_FOUND`

---

### 4.6 标签 (Tags)

---

#### GET /api/v1/recordings/{id}/tags

录音的标签（当前生效 + 历史版本）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / viewer（全量）；agent（仅自己） |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `view` | string | ❌ | `current` | `current`（当前生效）/ `history`（全版本）/ `facts`（append-only 原始行） |
| `tag_path` | string | ❌ | — | 按标签路径筛选（支持前缀如 `quality.*`） |

**响应 200 OK**（view=current）:
```json
{
  "recording_id": 1001,
  "view": "current",
  "tags": [
    {
      "tag_path": "quality.greeting",
      "tag_value": "pass",
      "version": 2,
      "prompt_version": "tag_prompt_v1"
    },
    {
      "tag_path": "sales.product_mention",
      "tag_value": "CS75_Plus",
      "version": 2,
      "prompt_version": "tag_prompt_v1"
    }
  ]
}
```

**响应 200 OK**（view=history）:
```json
{
  "recording_id": 1001,
  "view": "history",
  "tags": [
    {
      "tag_path": "quality.greeting",
      "tag_value": "fail",
      "version": 1,
      "prompt_version": "tag_prompt_v0",
      "source": "llm",
      "confidence": 0.85,
      "computed_at": "2026-07-15T10:44:00Z",
      "computed_by": null
    },
    {
      "tag_path": "quality.greeting",
      "tag_value": "pass",
      "version": 2,
      "prompt_version": "tag_prompt_v1",
      "source": "llm",
      "confidence": 0.92,
      "computed_at": "2026-07-16T08:00:00Z",
      "computed_by": null
    }
  ]
}
```

---

#### POST /api/v1/recordings/{id}/tags

对录音执行打标（LLM 自动判定或人工修正）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector |

**请求 Body**（LLM 自动打标）:
```json
{
  "mode": "auto",
  "tag_paths": ["quality.greeting", "sales.product_mention"],
  "prompt_version": "tag_prompt_v1"
}
```

**请求 Body**（人工修正）:
```json
{
  "mode": "manual",
  "tag_path": "quality.greeting",
  "tag_value": "pass",
  "reason": "人工核实，开场问候完整"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `mode` | string | ✅ | `auto` / `manual` | 打标模式 |
| `tag_paths` | string[] | mode=auto 时 ✅ | — | 指定打标路径（为空则全量） |
| `prompt_version` | string | mode=auto 时 ❌ | — | 指定 prompt 版本（为空用 active） |
| `tag_path` | string | mode=manual 时 ✅ | ≤ 255 字符 | 标签路径 |
| `tag_value` | string | mode=manual 时 ✅ | ≤ 255 字符 | 标签值 |
| `reason` | string | ❌ | — | 人工修正原因（写入 audit_log） |

**响应 200 OK**（mode=auto）:
```json
{
  "recording_id": 1001,
  "tagged": 2,
  "cached_hits": 1,
  "llm_calls": 1,
  "results": [
    {
      "tag_path": "quality.greeting",
      "tag_value": "pass",
      "version": 3,
      "confidence": 0.92,
      "cached": false
    },
    {
      "tag_path": "sales.product_mention",
      "tag_value": "CS75_Plus",
      "version": 3,
      "confidence": 0.95,
      "cached": true
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `tagged` | int | 本次打标数量 |
| `cached_hits` | int | LLM 缓存命中数（0 token） |
| `llm_calls` | int | 实际 LLM 调用数 |
| `results[].cached` | bool | 该标签是否命中缓存 |

**响应 200 OK**（mode=manual）:
```json
{
  "recording_id": 1001,
  "tag_path": "quality.greeting",
  "tag_value": "pass",
  "version": 4,
  "source": "manual",
  "computed_by": 1
}
```

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 404 | `RECORDING_NOT_FOUND` | — |
| 409 | `RECORDING_NOT_INDEXED` | 录音尚未 indexed，无法打标 |

---

#### POST /api/v1/tags/recompute

触发批量重算（Prompt 版本升级后对受影响的 recording 重打）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**请求 Body**:
```json
{
  "prompt_version": "tag_prompt_v2",
  "tag_paths": ["quality.greeting", "sales.product_mention"],
  "dry_run": false,
  "recording_ids": [1001, 1002]
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `prompt_version` | string | ✅ | ≤ 64 字符 | 目标 prompt 版本 |
| `tag_paths` | string[] | ❌ | — | 限定重算路径（为空则全量） |
| `dry_run` | bool | ❌ | false | dry-run = 只 diff 不写入 |
| `recording_ids` | int[] | ❌ | — | 限定录音范围（为空则查 `prompt_version < target` 的全部） |

**响应 200 OK**（dry_run=true）:
```json
{
  "dry_run": true,
  "affected_count": 45,
  "changed_count": 12,
  "unchanged_count": 33,
  "changes_preview": [
    {
      "recording_id": 1001,
      "tag_path": "quality.greeting",
      "old_value": "fail",
      "new_value": "pass"
    }
  ]
}
```

**响应 200 OK**（dry_run=false）:
```json
{
  "dry_run": false,
  "task_id": "recompute-2026-0716-001",
  "status": "pending",
  "affected_count": 45,
  "message": "Recompute task created. Poll /tags/recompute/{task_id} for status."
}
```

---

#### GET /api/v1/tags/recompute/{task_id} *(P1)*

重算任务状态查询。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**响应 200 OK**:
```json
{
  "task_id": "recompute-2026-0716-001",
  "status": "running",
  "prompt_version": "tag_prompt_v2",
  "total": 45,
  "processed": 20,
  "changed": 5,
  "cached_hits": 12,
  "llm_calls": 8,
  "started_at": "2026-07-16T08:00:00Z",
  "estimated_end": "2026-07-16T08:03:00Z"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | pending / running / done / failed |
| `processed` | int | 已处理数 |
| `changed` | int | 有变化数 |
| `cached_hits` | int | LLM 缓存命中数 |
| `llm_calls` | int | 实际 LLM 调用数 |

---

### 4.7 Prompt 版本管理 (Prompts)

---

#### GET /api/v1/prompts

Prompt 版本列表。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin / inspector / agent / viewer |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | string | ❌ | — | 按 prompt 名称筛选 |
| `active_only` | bool | ❌ | false | 仅返回 active 版本 |

**响应 200 OK**:
```json
{
  "items": [
    {
      "id": 1,
      "name": "entity_extraction",
      "version": "v0",
      "active": false,
      "changelog": "初版中文实体抽取 prompt",
      "created_by": 1,
      "created_at": "2026-07-01T00:00:00Z"
    },
    {
      "id": 3,
      "name": "entity_extraction",
      "version": "v1",
      "active": true,
      "changelog": "增加竞品对比实体类型",
      "created_by": 1,
      "created_at": "2026-07-10T00:00:00Z"
    }
  ]
}
```

---

#### POST /api/v1/prompts

新建 Prompt 版本。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**请求 Body**:
```json
{
  "name": "entity_extraction",
  "version": "v2",
  "content": "你是门店录音质检专家...\n实体类型：{entity_types}\n输入：{input_text}\n...",
  "changelog": "优化金融政策识别准确率",
  "activate": false
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `name` | string | ✅ | ≤ 255 字符 | Prompt 名称 |
| `version` | string | ✅ | ≤ 64 字符 | 版本标识 |
| `content` | string | ✅ | 非空 | Prompt 模板内容 |
| `changelog` | string | ❌ | — | 变更说明 |
| `activate` | bool | ❌ | false | 创建后立即激活（触发重算） |

**响应 201 Created**:
```json
{
  "id": 4,
  "name": "entity_extraction",
  "version": "v2",
  "active": false,
  "changelog": "优化金融政策识别准确率",
  "created_by": 1,
  "created_at": "2026-07-16T08:00:00Z"
}
```

**错误响应**:

| 状态码 | code | 场景 |
|---|---|---|
| 409 | `DUPLICATE_PROMPT_VERSION` | (name, version) 已存在 |

---

#### GET /api/v1/prompts/{id}

Prompt 详情（含 content）。

**响应 200 OK**:
```json
{
  "id": 4,
  "name": "entity_extraction",
  "version": "v2",
  "content": "你是门店录音质检专家...",
  "changelog": "优化金融政策识别准确率",
  "active": false,
  "created_by": 1,
  "created_at": "2026-07-16T08:00:00Z"
}
```

**错误响应**: `404 PROMPT_NOT_FOUND`

---

#### POST /api/v1/prompts/{id}/activate

切换生效版本（触发 delta 重算）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin |

**请求 Body**:
```json
{
  "trigger_recompute": true,
  "dry_run": false
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `trigger_recompute` | bool | ❌ | true | 是否触发批量重算 |
| `dry_run` | bool | ❌ | false | true = 只 diff 不写入（返回影响预览） |

**响应 200 OK**（trigger_recompute=true, dry_run=false）:
```json
{
  "prompt_id": 4,
  "name": "entity_extraction",
  "version": "v2",
  "active": true,
  "previous_active_id": 3,
  "recompute_task_id": "recompute-2026-0716-002",
  "affected_count": 45,
  "message": "Prompt activated. Recompute task created."
}
```

**响应 200 OK**（dry_run=true）:
```json
{
  "prompt_id": 4,
  "version": "v2",
  "dry_run": true,
  "affected_count": 45,
  "changed_count": 12,
  "unchanged_count": 33,
  "changes_preview": [
    {
      "recording_id": 1001,
      "tag_path": "quality.greeting",
      "old_value": "fail",
      "new_value": "pass"
    }
  ]
}
```

---

### 4.8 标签统计 (Stats)

---

#### GET /api/v1/tags/stats

多级标签聚合查询（看板用）。

| 属性 | 值 |
|---|---|
| **认证** | ✅ Bearer token |
| **角色要求** | admin（全租户）/ inspector（本租户）/ viewer（本租户）/ agent（仅自己） |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `store_id` | string | ❌ | — | 按门店下钻 |
| `agent_name` | string | ❌ | — | 按坐席下钻 |
| `tag_path` | string | ❌ | — | 按标签路径筛选（支持前缀 `quality.*`） |
| `tag_value` | string | ❌ | — | 按标签值筛选 |
| `group_by` | string | ❌ | `tag_path` | 聚合维度：`store_id` / `agent_name` / `tag_path` / `tag_value` |

**响应 200 OK**:
```json
{
  "dimensions": ["tag_path", "tag_value"],
  "items": [
    {
      "tag_path": "quality.greeting",
      "tag_value": "pass",
      "tag_count": 128
    },
    {
      "tag_path": "quality.greeting",
      "tag_value": "fail",
      "tag_count": 28
    },
    {
      "tag_path": "sales.product_mention",
      "tag_value": "CS75_Plus",
      "tag_count": 45
    }
  ],
  "total_records": 156
}
```

---

### 4.9 健康检查 (Health)

---

#### GET /api/v1/health/readiness

就绪检查——DB + adapters 可达性。

| 属性 | 值 |
|---|---|
| **认证** | 无（k8s probe 用） |
| **角色要求** | 无 |

**响应 200 OK**（全部就绪）:
```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "adapters": {
      "vad": "ok",
      "asr": "ok",
      "strong_llm": "ok",
      "weak_llm": "ok",
      "embed": "ok"
    },
    "graph_store": "ok",
    "file_index": "ok"
  },
  "version": "0.3.0"
}
```

**响应 503 Service Unavailable**（有组件不可达）:
```json
{
  "status": "not_ready",
  "checks": {
    "database": "ok",
    "adapters": {
      "strong_llm": "error: connection refused"
    }
  }
}
```

---

## 5. RBAC 权限矩阵 (RBAC Matrix)

> 图例：✅ 允许 · ❌ 禁止（403） · 🔒 仅自己（agent 数据范围限制）

| 端点 (Endpoint) | 方法 | Admin | Inspector | Agent | Viewer |
|---|:---:|:---:|:---:|:---:|:---:|
| `/auth/login` | POST | — | — | — | — |
| `/auth/refresh` | POST | — | — | — | — |
| `/auth/me` | GET | ✅ | ✅ | ✅ | ✅ |
| `/recordings` | POST | ✅ | ❌ | ❌ | ❌ |
| `/recordings` | GET | ✅ | ✅ | 🔒 | ✅ |
| `/recordings/{id}` | GET | ✅ | ✅ | 🔒 | ✅ |
| `/recordings/{id}/status` | GET | ✅ | ✅ | 🔒 | ✅ |
| `/recordings/{id}/reindex` | POST | ✅ | ❌ | ❌ | ❌ |
| `/recordings/{id}/segments` | GET | ✅ | ✅ | 🔒 | ✅ |
| `/query` | POST | ✅ | ✅ | ✅ | ✅ |
| `/graph/explore` | GET | ✅ | ✅ | ✅ | ✅ |
| `/graph/entity/{name}` | GET | ✅ | ✅ | ✅ | ✅ |
| `/graph/subgraph` | GET | ✅ | ✅ | ✅ | ✅ |
| `/graph/path` | GET | ✅ | ✅ | ✅ | ✅ |
| `/recordings/{id}/tags` | GET | ✅ | ✅ | 🔒 | ✅ |
| `/recordings/{id}/tags` | POST | ✅ | ✅ | ❌ | ❌ |
| `/tags/recompute` | POST | ✅ | ❌ | ❌ | ❌ |
| `/tags/recompute/{task_id}` | GET | ✅ | ❌ | ❌ | ❌ |
| `/prompts` | GET | ✅ | ✅ | ✅ | ✅ |
| `/prompts` | POST | ✅ | ❌ | ❌ | ❌ |
| `/prompts/{id}` | GET | ✅ | ✅ | ✅ | ✅ |
| `/prompts/{id}/activate` | POST | ✅ | ❌ | ❌ | ❌ |
| `/tags/stats` | GET | ✅(全租户) | ✅(本租户) | 🔒(仅自己) | ✅(本租户) |
| `/health/readiness` | GET | — | — | — | — |

> **跨租户隔离规则**：所有带 `{id}` 的端点，若资源 `tenant_id != JWT.tid`，统一返回 **404 Not Found**（而非 403），不暴露资源存在性。

---

## 6. 标签治理流程 (Tag Governance Flow)

### 6.1 三层数据流

```
                    ┌─────────────────────────────────────────┐
                    │          Prompt v2 上线 (activate)         │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │  MySQL 查 prompt_version < v2 的 recording  │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
          ┌────────>|│            逐 recording 重打 tag            │
          │         ││  ┌─────────────────────────────────┐    │
          │         ││  │ LLM 缓存命中？                   │    │
          │         ││  │  YES → 0 token，直接用旧结果     │    │
          │         ││  │  NO  → 调 LLM（新 prompt）       │    │
          │         ││  └─────────────────────────────────┘    │
          │         └──────────────────┬──────────────────────┘
          │                            │
          │            ┌───────────────▼───────────────┐
          │            │   Layer 1: tag_facts INSERT     │
          │            │   append-only, version+1        │
          │            │   (不动 v1 行)                   │
          │            └───────────────┬───────────────┘
          │                            │
          │            ┌───────────────▼───────────────┐
          │            │   diff: v2 vs v1                │
          │            │   tag_value 相同？               │
          │            │  YES → 丢弃（无变化）            │
          │            │  NO  → 入 dirty 队列             │
          │            └───────────────┬───────────────┘
          │                            │ dirty only
          │            ┌───────────────▼───────────────┐
          │            │   Layer 2: tag_current UPSERT   │
          │            │   = MAX(version) per path       │
          │            └───────────────┬───────────────┘
          │                            │
          │            ┌───────────────▼───────────────┐
          │            │   Layer 3: tag_stats DELTA      │
          │            │   tag_count -= old_value_count  │
          │            │   tag_count += new_value_count  │
          │            │   (增量，不全量重算)             │
          │            └───────────────┬───────────────┘
          │                            │
          │            ┌───────────────▼───────────────┐
          └────────────│  recording.prompt_version = v2  │
                       │  还有下一条？                    │
                       └────────────────────────────────┘
```

### 6.2 幂等条件矩阵

| 场景 | prompt_version | input_hash | LLM 缓存 | tag_facts 写入 | tag_stats 更新 |
|---|---|---|---|---|---|
| **同 prompt 重打** | 不变 | 不变 | ✅ 命中（0 token） | ❌ 不写（version 已存在） | ❌ 不更新 |
| **prompt 升级 · 值不变** | 变 | 变 | ❌ 未命中 | ✅ 写新 version | ❌ 不更新（diff 相同丢弃） |
| **prompt 升级 · 值变化** | 变 | 变 | ❌ 未命中 | ✅ 写新 version | ✅ delta 更新 |
| **人工修正** | 不变 | — | — | ✅ 写新 version (source=manual) | ✅ delta 更新 |
| **滞后期望数据到达** | 不变 | 变 | 视情况 | ✅ 写新 version | ✅ delta 更新 |

### 6.3 重算策略

| 触发场景 | 影响范围 | 策略 |
|---|---|---|
| 单条人工修正 | 1 条 recording | 增量 delta：聚合 −旧 +新 |
| prompt/规则升级 | 一批 recording | version-gate：重打→diff→只 commit 变化→增量聚合 |
| 滞后数据到达 | 某时间窗 | 只重算该 time_window 聚合 |
| 分类法变更 | 全部 | 全量重算（罕见批跑，M3 不实现） |

---

## 7. 验收标准 (Acceptance Criteria)

### 7.1 认证中间件

| ID | 验收项 | 通过条件 |
|---|---|---|
| AC-AUTH-01 | JWT 签发 | POST /auth/login 返回有效 access_token + refresh_token |
| AC-AUTH-02 | JWT 校验 | 带 token 访问受保护端点返回 200；不带返回 401 |
| AC-AUTH-03 | 过期 token | 过期 access_token 返回 401 `TOKEN_EXPIRED` |
| AC-AUTH-04 | Refresh | POST /auth/refresh 用有效 refresh_token 换新 access_token |
| AC-AUTH-05 | 角色越权 | viewer 访问 POST /recordings 返回 403 `FORBIDDEN` |
| AC-AUTH-06 | Agent 数据范围 | agent 只看到 `agent_name == self.name` 的录音 |
| AC-AUTH-07 | 跨租户 404 | 租户 A 的 token 访问租户 B 的 recording → 404（非 403） |

### 7.2 标签治理

| ID | 验收项 | 通过条件 |
|---|---|---|
| AC-TAG-01 | tag_facts append-only | 同 recording + tag_path 的 version 递增，旧行不被修改 |
| AC-TAG-02 | tag_current = MAX(version) | tag_current 总是反映最新 version 的 tag_value |
| AC-TAG-03 | tag_stats delta 正确 | 重算后 tag_count = 旧 count − old_value + new_value |
| AC-TAG-04 | LLM 缓存幂等 | 同 prompt 重打 → cached_hits > 0, llm_calls = 0 |
| AC-TAG-05 | prompt 升级 diff | activate v2 → dry_run 返回 changed_count + unchanged_count |
| AC-TAG-06 | 增量重算只更新 dirty | recompute 后只有值变化的 tag_stats 行被更新 |
| AC-TAG-07 | 人工修正 | POST tags (mode=manual) → source=manual, version+1, tag_stats delta |

### 7.3 业务路由

| ID | 验收项 | 通过条件 |
|---|---|---|
| AC-API-01 | 录音上传 | POST /recordings → 201 + status=queued + pipeline 后台触发 |
| AC-API-02 | 录音列表筛选 | store_id / status / agent_name / time_range 筛选均生效 |
| AC-API-03 | 录音详情 | GET /recordings/{id} 含 segments_count + current_tags |
| AC-API-04 | 问答检索 | POST /query → answer 非空 + citations 非空 + retrieval_stats 填充 |
| AC-API-05 | 时间过滤 | query 带 time_range → 结果中 recorded_at 均在范围内 |
| AC-API-06 | 图谱浏览 | GET /graph/explore → nodes + edges 非空（有数据时） |
| AC-API-07 | 图谱子图 | GET /graph/subgraph → 返回 N-hop 邻居子图 |
| AC-API-08 | 边置信度染色 | graph 响应 edge 含 confidence 字段（EXTRACTED/INFERRED/AMBIGUOUS） |
| AC-API-09 | Prompt CRUD | 创建 → 查询 → 激活 → 重算触发，全链路通 |
| AC-API-10 | 标签统计下钻 | GET /tags/stats 按 store_id / agent_name / tag_path 聚合 |
| AC-API-11 | 就绪检查 | GET /health/readiness → DB + adapters 状态 |
| AC-API-12 | 分页 | 所有列表端点 page/page_size 分页正确 |

### 7.4 集成测试

| ID | 验收项 | 通过条件 |
|---|---|---|
| AC-TEST-01 | 端到端 ingestion | upload → status 轮询至 indexed → segments 可查 → tags 可查 |
| AC-TEST-02 | 端到端 query | query → answer + citations 非空 → citation 溯源链完整 |
| AC-TEST-03 | 端到端 recompute | 打标 v1 → activate v2 → recompute → tag_current 更新 → stats delta 正确 → 缓存命中 |
| AC-TEST-04 | RBAC 全矩阵 | 4 角色 × 关键端点 ≥ 20 组合，权限拒绝均返回 403 |
| AC-TEST-05 | 跨租户隔离 | 租户 A 数据对租户 B 不可见（404） |

### 7.5 通用质量

| ID | 验收项 | 通过条件 |
|---|---|---|
| AC-QUAL-01 | 错误格式统一 | 所有 4xx/5xx 返回 `{ "error": { "code", "message" } }` |
| AC-QUAL-02 | OpenAPI 文档 | `/docs` 可访问，所有端点有描述 + schema |
| AC-QUAL-03 | ruff + mypy | `ruff check && ruff format --check && mypy` 全过 |
| AC-QUAL-04 | 测试全过 | pytest 0 failed（M1 + M2 + M3 全部） |
| AC-QUAL-05 | 覆盖率 | M3 新增代码覆盖率 ≥ 85% |
| AC-QUAL-06 | 无硬编码密钥 | JWT_SECRET / DB_PASSWORD 从环境变量读取 |

---

## 8. 待确认问题 (Open Questions)

| # | 问题 | 影响模块 | 默认假设（如不确认则采用） |
|---|---|---|---|
| Q1 | **密码存储与验证方案**：M3 用什么密码哈希？bcrypt / argon2？User 表目前无 password 字段，需新增 migration。还是 M3 先用预置 token（开发模式）跳过密码？ | auth | 默认：M3 新增 User.password_hash 字段（bcrypt），login 端点校验。如主理人倾向"先跳过密码用 dev token"，则改为固定 seed token。**需主理人决策**。 |
| Q2 | **Pipeline worker 执行方式**：APScheduler 进程内后台线程 vs 独立 Celery worker？M3 目标是"可用"，APScheduler 进程内是否够？ | tags/recompute, recordings | 默认：APScheduler 进程内（与 config.py `pipeline_poll_seconds` 一致），Phase 4 再拆独立 worker。 |
| Q3 | **录音上传方式**：M3 是注册文件 path（服务端可访问的路径），还是支持 multipart 文件上传？任务描述说"上传/列表/详情"，需明确。 | recordings | 默认：M3 只做 path 注册（POST /recordings 传 path 字符串），不做 multipart。Phase 4 加 multipart + 分片上传。**需主理人确认**。 |
| Q4 | **重算任务持久化**：recompute task 状态存哪里？MySQL 新建表？还是 APScheduler jobstore？ | tags/recompute | 默认：MySQL 新建 `recompute_tasks` 表（task_id / status / prompt_version / progress / started_at / finished_at）。简单轻量。 |
| Q5 | **Agent 数据范围限制实现层**：Agent 只看自己的录音，是在中间件层注入 `agent_name` 过滤，还是在每个 router 内单独处理？ | auth/middleware | 默认：中间件注入 `request.state.agent_filter = user.name`（仅 agent 角色），各 router 查询时读取该值加 WHERE 条件。 |
| Q6 | **Graph 数据来源**：API 返回的图谱数据是从 NetworkX GraphML 加载，还是从 MySQL 查？M2 的 graph_store 是文件级的，API 层如何桥接？ | graph router | 默认：API 层初始化 NetworkXGraphStore（per-tenant），从 GraphML 文件 load 到内存，查询时读内存图。写操作（reindex）通过 pipeline worker 更新图文件。 |
| Q7 | **标签 path 规范**：tag_path 的层级规范是什么？如 `quality.greeting` / `sales.product_mention`。需要预定义 taxonomy 还是由 prompt 动态产出？ | tags | 默认：M3 不预定义 taxonomy，tag_path 由打标 prompt 动态产出。统计查询支持前缀通配（`quality.*`）。 |
| Q8 | **Tenant 种子数据**：M3 测试需要预置哪些租户和用户？DESIGN.md 提到"chang_an"租户，需要确定测试种子。 | 测试 | 默认：测试种子 2 租户（tenant_a / tenant_b），每租户 4 用户（admin/inspector/agent/viewer）。 |

---

**文档结束 (End of Document)**

> 本 PRD 是 M3 工程实现的唯一需求权威源。架构师设计文档（M3-Arch）和工程师代码实现（M3-Impl）必须以本文档为准。任何偏离需经 PM 确认后更新本文档。
