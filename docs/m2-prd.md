# AudioGraphy M2 PRD — core/ 算法模块实现

> **里程碑**: M2 · Phase 1 核心算法层（go/no-go 关卡）
> **版本**: v1.0 · 2026-07
> **作者**: 许清楚 (Xu) · 产品经理
> **权威源**: `docs/DESIGN.md` §3-5
> **前置里程碑**: M1.1-M1.5（repo / docker / alembic / config / mock adapters / ORM models · 190 测试全过 · 覆盖率 ~97%）

---

## 1. 产品目标 (Product Goals)

M2 是 AudioGraphy 的 **go/no-go 关卡**——证明"门店录音 → 图谱化 → 图谱驱动检索 → 带溯源回答"的完整链路在 mock adapter 下端到端跑通。跑通即可证明 AudioRAG 可行性，进入 Phase 2 音频增强。

| # | 目标 (Goal) | 衡量标准 (Measurable Criteria) |
|---|---|---|
| G1 | **核心算法层落地**：实现 DESIGN.md §3 定义的 5 个 core 模块 + §7 定义的 3 个 storage 模块，构成完整索引/查询流水线 | 8 个模块全部可单测 + 端到端集成测试通过 |
| G2 | **三级溯源链贯通**：从最终回答的引用能反查到具体录音、具体段、具体录制时间 | citation 链 entity → chunk → segment → recorded_at 完整且可验证 |
| G3 | **开源就绪**：docstring / 类型注解 / ruff / pytest 全过，不依赖外部真实服务（mock + testcontainers） | `pytest` 一条命令全过、覆盖率 ≥ 85%、`ruff check && ruff format --check` 全过、mypy strict 全过 |

### 1.1 M2 不做什么 (Out of Scope)

| 不做 | 原因 | 何时做 |
|---|---|---|
| 接真实 ASR/LLM/Embed 服务 | M2 用 mock adapter 验证算法逻辑，真服务接入是 Phase 2 | Phase 2 |
| 说话人节点 (CAM++) | DESIGN.md §4.4.2 Level 3 强增量 | Phase 2 |
| 音频嵌入通道 (CLAP) | DESIGN.md §4.4.1 Level 2 | Phase 2 |
| 标签版本化 + 增量重算 | M2 聚焦 core 算法层，标签治理是 Phase 3 | Phase 3 |
| FastAPI 路由 / Arco 前端 | M2 是纯算法层，API/UI 在 Phase 3 | Phase 3 |
| 流式索引 | DESIGN.md §9 明确先离线 | Phase 4（可选）|

---

## 2. 用户故事 (User Stories)

| # | 角色 (Role) | 故事 (Story) |
|---|---|---|
| US1 | **开源贡献者 (OSS Contributor)** | 作为一个想复用 AudioGraphy 图谱内核的开发者，我希望 clone 仓库后 `pytest` 一条命令全过、不依赖任何外部服务，这样我能快速理解算法逻辑并开始二次开发。 |
| US2 | **算法工程师 (Algorithm Engineer)** | 作为一个负责实体抽取优化的工程师，我希望 extractor 的 prompt 模板与解析逻辑分离、且 Gleaning 轮数可配，这样我能独立迭代 prompt 而不动核心代码、并量化对比召回提升。 |
| US3 | **质检员 (Quality Inspector)** | 作为一个门店录音质检员，我希望系统能回答"7 月 1-15 日哪些接待提到了 CS75 Plus 的金融政策"并精确溯源到具体录音的某个时间段，这样我能回到现场核实。 |
| US4 | **架构师 (Architect)** | 作为一个负责技术选型的架构师，我希望看到双通道检索（naive + 图谱）+ LLM as-judge 过滤的完整实现，且暴力余弦在小规模下等价 NanoVectorDB，这样我能判断 Phase 3 是否需要升级独立向量库。 |
| US5 | **QA 工程师 (QA Engineer)** | 作为一个负责测试的工程师，我希望每个 core 模块都有独立的单测、且有端到端集成测试覆盖完整索引+查询链路，这样我能快速定位回归问题。 |

---

## 3. 需求池 (Requirements Pool)

### 3.1 P0 — Must Have（go/no-go 阻塞项）

| ID | 模块 | 文件 | 描述 |
|---|---|---|---|
| P0-1 | chunker | `core/chunker.py` | VAD 切分 + ASR 转写 + 段→chunk 层次打包 + 三级溯源链 |
| P0-2 | extractor | `core/extractor.py` | GraphRAG 分隔符协议实体抽取 + Gleaning 补抽 + 中文 prompt |
| P0-3 | graph | `core/graph.py` | 实体/关系跨 chunk 合并进图 + 边置信度标签 EXTRACTED/INFERRED/AMBIGUOUS |
| P0-4 | retrieval | `core/retrieval.py` | 双通道召回（naive 文本块 + 图谱邻居）+ union 去重 + 时间过滤 |
| P0-5 | rerank | `core/rerank.py` | LLM as-judge 过滤 + 精化重排（关键词 + 高精度重转写 + 精描述） |
| P0-6 | mysql_vector | `storage/mysql_vector.py` | 暴力余弦 top-k 检索（entity + chunk 双表）+ float32↔BLOB 序列化 |
| P0-7 | graph_networkx | `storage/graph_networkx.py` | NetworkX 图 CRUD + GraphML 持久化 + relation_counts 查询 |
| P0-8 | file_index | `storage/file_index.py` | working_dir JSON 读写 + LLM 响应缓存 + checkpoint flush |
| P0-9 | 测试 | `tests/core/` + `tests/storage/` | 每个模块单测 + 端到端集成测试（索引 + 查询全链路） |
| P0-10 | 开源规范 | 全部 core/storage 文件 | docstring（模块级 + 类/函数级）+ 类型注解 100% + ruff/mypy 全过 |

### 3.2 P1 — Should Have

| ID | 模块 | 文件 | 描述 |
|---|---|---|---|
| P1-1 | prompt 模板 | `prompts/entity_zh.md` | 中文实体抽取 prompt（汽车销售领域，3 处中文化改动） |
| P1-2 | prompt 注册 | `prompts/versions.yaml` | prompt_version 注册表 |
| P1-3 | 中文归一 | `core/extractor.py` 内 | 别名表 + 编辑距离聚类，缓解近重名实体 |
| P1-4 | 评估指标 | `tests/core/test_metrics.py` | A0 格式合规（parse 成功率 / 空抽取率 / 近重名实体率） |
| P1-5 | 错误恢复 | 各 core 模块 | adapter 失败重试 + LLM 输出解析降级 + 空结果处理 |

### 3.3 P2 — Nice to Have

| ID | 模块 | 文件 | 描述 |
|---|---|---|---|
| P2-1 | 性能 | `storage/mysql_vector.py` | 批量 cosine 矩阵化（numpy），减少 Python 循环 |
| P2-2 | 流式预留 | `core/chunker.py` | chunker 接口预留滚动窗口扩展点（不实现，仅留 hook） |
| P2-3 | 缓存清理 | `storage/file_index.py` | LLM cache 过期清理机制（按大小/时间） |

---

## 4. 模块需求规格 (Module Specifications)

> 约定：所有 core 模块依赖 `AdapterBundle`（vad / asr / strong_llm / weak_llm / embed），不直接依赖具体 adapter 实现。所有公共 API 使用 `async def`，与 adapter Protocol 一致。返回类型用 `frozen=True, slots=True` dataclass，与 `adapters/protocols.py` 风格一致。

### 4.1 chunker.py — VAD 切分 + ASR 转写 + 段→chunk 层次打包

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/core/chunker.py` |
| **职责** | 录音音频 → VAD 分段 → 逐段 ASR 转写 → 按 token 预算打包成 chunk → 写入 segments/chunks 表 + file_index |
| **DESIGN.md 来源** | §3.2 段→chunk 层次打包 + 三级溯源链；§4.2 VAD 替代文件切分 |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class SegmentRecord:
    """VAD + ASR 产出的一条段记录。"""
    idx: int                    # 段序号（recording 内唯一）
    start_sec: float
    end_sec: float
    transcript: str
    speaker: str | None         # M2 mock 不分说话人，留 None
    vad_conf: float

@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """打包后的 chunk 记录（对应 Chunk ORM）。"""
    segment_ids: list[int]      # 溯源：chunk → segments
    text: str                   # 拼接的段 transcript
    token_n: int                # token 计数
    content_hash: str           # SHA-256(text)，用于幂等去重

@dataclass(frozen=True, slots=True)
class ChunkerOutput:
    """chunker 完整输出。"""
    recording_id: int
    segments: list[SegmentRecord]
    chunks: list[ChunkRecord]

class Chunker:
    """VAD → ASR → chunking 流水线。

    Args:
        bundle: AdapterBundle（使用 vad + asr）
        token_budget: 单 chunk 最大 token 数（默认 1200，DESIGN.md §3.2）
        overlap_tokens: chunk 间重叠 token 数（默认 0，流式预留）
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        token_budget: int = 1200,
        overlap_tokens: int = 0,
    ) -> None: ...

    async def process_recording(
        self,
        recording_id: int,
        audio_path: str,
        recorded_at: datetime | None,
        *,
        tenant_id: str = "default",
    ) -> ChunkerOutput: ...
```

#### 数据流

| 步骤 | 输入 | 处理 | 输出 | 存储 |
|---|---|---|---|---|
| 1. VAD 切分 | `audio_path` | `bundle.vad.segment(audio_path)` | `Sequence[VADSegment]` | 内存 |
| 2. ASR 转写 | 每个 VADSegment 的音频片段 | `bundle.asr.transcribe(segment_audio)` | `ASRResult` per segment | 内存 |
| 3. 段构建 | VADSegment + ASRResult | 组装 `SegmentRecord(idx, start, end, transcript, speaker=None, vad_conf)` | `list[SegmentRecord]` | → MySQL `segments` 表 |
| 4. chunk 打包 | `list[SegmentRecord]` | 按 `token_budget` 累加，多个段打包进一个 chunk | `list[ChunkRecord]` | → MySQL `chunks` 表 + file_index `kv_store_text_chunks.json` |

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **token 预算打包** | 按 token 数（中文按字符数 / 2 近似）累加段 transcript，超过 `token_budget` 则截断开新 chunk。每个 chunk 记录 `segment_ids[]` 数组 | §3.2 |
| **三级溯源链第一环** | chunk.segment_ids → segments.idx → segment.transcript + recording.recorded_at | §3.2 |
| **content_hash 幂等** | `SHA-256(text)`，对应 chunks 表 `UNIQUE(tenant_id, content_hash)`，重跑同录音不重复建 chunk | §7.5 |
| **recorded_at 透传** | 从 recording 元数据传入 `recorded_at`，写入 file_index `kv_store_video_segments.json`，不用 `time.time()` | §5.3 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| VAD adapter 抛异常 | 向上传播，pipeline_state 置 `error`，recording.status 置 `failed` |
| ASR adapter 抛异常（单段） | 该段 transcript 置空字符串 `""`，vad_conf 保留，继续处理后续段（不阻塞整条录音） |
| 音频文件不存在 | `FileNotFoundError` 向上传播 |
| chunk text 为空（所有段 ASR 都失败） | 返回空 chunks 列表 + warning 日志，不抛异常（上层决定是否标 failed） |

---

### 4.2 extractor.py — 中文实体抽取 + Gleaning

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/core/extractor.py` |
| **职责** | 每个 chunk 并发抽实体/关系 → Gleaning 补抽提升召回 → 正则解析 GraphRAG 分隔符协议格式 → 输出结构化实体/关系列表 |
| **DESIGN.md 来源** | §3.1 图谱知识索引；§5.1 实体抽取 prompt 中文化 |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    """单 chunk 抽取出的一个实体。"""
    name: str
    type: str                   # 领域类型：客户/坐席/车型/价格方案/金融政策/优惠权益/竞品/预约事件
    description: str
    chunk_id: int               # 溯源：实体 → chunk
    recording_id: int

@dataclass(frozen=True, slots=True)
class ExtractedRelation:
    """单 chunk 抽取出的一条关系。"""
    source_name: str
    target_name: str
    relation: str               # 关系描述（如"推荐"/"询问"/"对比"）
    description: str
    weight: float               # 默认 1.0
    confidence: EdgeConfidence  # 首轮抽取 = EXTRACTED；Gleaning 补抽 = INFERRED
    chunk_id: int
    recording_id: int

@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """单 chunk 的完整抽取结果。"""
    chunk_id: int
    recording_id: int
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    parse_success: bool         # A0 指标：LLM 输出是否可正则解析
    gleaning_rounds: int        # 实际执行的 Gleaning 轮数

class EntityExtractor:
    """GraphRAG 风格实体抽取 + Gleaning 补抽。

    Args:
        bundle: AdapterBundle（使用 strong_llm）
        prompt_template: 实体抽取 prompt 模板字符串（含 {tuple_delimiter} /
            {record_delimiter} / {completion_delimiter} / {entity_types} /
            {input_text} 占位符）
        gleaning_rounds: Gleaning 补抽轮数（默认 1 = 首轮 + 1 次强制补抽）
        entity_types: 领域实体类型列表
        max_gleaning_retry: 单轮 Gleaning LLM 调用最大重试次数（默认 2）
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        *,
        prompt_template: str,
        gleaning_rounds: int = 1,
        entity_types: tuple[str, ...] = (
            "客户", "坐席", "车型", "价格方案",
            "金融政策", "优惠权益", "竞品", "预约事件",
        ),
        max_gleaning_retry: int = 2,
    ) -> None: ...

    async def extract_from_chunk(
        self,
        chunk_id: int,
        chunk_text: str,
        recording_id: int,
    ) -> ExtractionResult: ...

    async def extract_from_chunks(
        self,
        chunks: Sequence[tuple[int, str, int]],  # (chunk_id, text, recording_id)
        *,
        concurrency: int = 4,
    ) -> list[ExtractionResult]: ...
```

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **GraphRAG 分隔符协议** | LLM 输出格式：`("实体","名称","类型")<tuple_delim>("关系","源","关系","目标")<record_delim>...<completion_delim>`。代码用正则解析，比 JSON 对 LLM 更鲁棒。分隔符常量：`tuple_delimiter = "<|>"`、`record_delimiter = "##"`、`completion_delimiter = "<|COMPLETE|>"` | §3.1 |
| **Gleaning 补抽** | 首轮抽取后，把已抽实体列表拼入 prompt，问 LLM"是否遗漏了实体/关系"，强制补抽 1 轮。补抽出的关系 confidence 标 `INFERRED`。若 LLM 回复无新实体则提前终止 | §3.1 |
| **LLM 缓存** | 每次 `complete()` 传 `cache_key = prompt_hash`。同 prompt 重跑命中缓存、0 token。对应 file_index `kv_store_llm_response_cache.json` | §7.3 |
| **中文实体归一** | 抽取后加一层归一：① 别名表查表（如 `CS75PLUS` → `CS75 Plus`）；② 编辑距离 ≤ 2 的实体名聚类（P1，可降级为不做） | §5.2 |
| **A0 指标采集** | 每次抽取记录 `parse_success`（正则能否解析）、`entities` 数量、空抽取率 | §8.1 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| LLM adapter 抛异常 | 重试 `max_gleaning_retry` 次（指数退避）；仍失败则该 chunk 返回 `ExtractionResult(parse_success=False, entities=[], relations=[])` |
| LLM 输出无法正则解析 | `parse_success=False`，尝试宽松正则（容错降级）提取能解析的部分；完全无法解析则返回空结果 |
| Gleaning 轮 LLM 回复无新实体 | 提前终止 Gleaning，`gleaning_rounds` 记录实际轮数 |
| chunk_text 为空 | 直接返回空 ExtractionResult，不调 LLM |

---

### 4.3 graph.py — 实体/关系合并进图 + 边置信度

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/core/graph.py` |
| **职责** | 跨 chunk 按名字去重合并实体/关系 → 跨录音全局合并 → 写入 NetworkX 图 → 边打置信度标签 |
| **DESIGN.md 来源** | §3.1 合并规则；§3.4 边置信度标签（借鉴 Graphify） |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class GraphNode:
    """图谱节点（合并后）。"""
    entity_id: str              # 归一化后的实体名（作为节点 ID）
    name: str
    type: str                   # 多数投票后的类型
    description: str            # 去重拼接后的描述
    source_ids: list[str]       # 溯源：["{recording_id}_{chunk_id}", ...]
    recording_ids: list[int]    # 出现在哪些录音
    degree: int                 # 连接数（god node 排序用）

@dataclass(frozen=True, slots=True)
class GraphEdge:
    """图谱边（合并后）。"""
    source: str                 # source entity_id
    target: str                 # target entity_id
    relation: str
    weight: float               # 累加权重（提及越多越强）
    confidence: EdgeConfidence  # EXTRACTED / INFERRED / AMBIGUOUS
    confidence_score: float     # 1.0 for EXTRACTED; 0.0-1.0 for INFERRED; None for AMBIGUOUS
    source_ids: list[str]       # 溯源到 chunks

@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """图谱构建后的快照。"""
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_entities: int
    total_relations: int
    cross_recording_entities: int   # 跨录音合并的实体数

class GraphBuilder:
    """跨 chunk / 跨录音实体关系合并进 NetworkX 图。

    Args:
        graph_store: NetworkXGraphStore 实例
    """

    def __init__(self, graph_store: NetworkXGraphStore) -> None: ...

    async def build_from_extractions(
        self,
        extractions: Sequence[ExtractionResult],
        *,
        tenant_id: str = "default",
    ) -> GraphSnapshot: ...
```

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **实体类型多数投票** | 同名实体出现在多个 chunk，类型可能不一致 → 用 `Counter` 取出现次数最多的类型 | §3.1 |
| **描述去重拼接** | 同名实体的多个描述去重后拼接；超长（> 512 字符）则调 weak_llm 摘要 | §3.1 |
| **边权重累加** | 同一对 (source, target, relation) 在多个 chunk 出现 → weight 累加。提及越多越强 | §3.1 |
| **跨录音合并** | 实体按 `entity_id`（归一化名）全局合并，`source_ids` 列表追加多个录音的 chunk 引用。这是 Cross-Audio Understanding 的机制 | §3.1 |
| **边置信度标签** | ① `EXTRACTED`：transcript 中明确存在的关系（首轮抽取），score=1.0 ② `INFERRED`：Gleaning 补抽或跨段合并推断的关系，score=weight 归一化 ③ `AMBIGUOUS`：source/target 实体名归一后碰撞（如两个不同"客户"被合并），待人工审核 | §3.1, §3.4 |
| **GraphML 持久化** | 合并完成后调 `graph_store.save()` 写入 `graph_chunk_entity_relation.graphml`，边/节点带 confidence 属性 | §7.2 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| graph_store save 失败 | 向上传播，但内存中的 GraphSnapshot 仍返回（数据不丢，下次 flush 可补偿） |
| 描述摘要 LLM 失败 | 降级为截断（取前 512 字符），不阻塞图谱构建 |
| 实体归一后碰撞（同名不同实体） | 该实体节点标 `AMBIGUOUS`，关联边也标 `AMBIGUOUS`，不丢弃 |
| extractions 为空列表 | 返回空 GraphSnapshot，warning 日志 |

---

### 4.4 retrieval.py — 双通道检索 + 时间过滤

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/core/retrieval.py` |
| **职责** | query → embedding → 双通道召回（naive 文本块 + 图谱邻居）→ union 去重 → 时间过滤 → 按 recorded_at 排序 |
| **DESIGN.md 来源** | §3.3 双通道检索 + 重排（四阶段流水线的阶段 1-2） |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class CandidateSegment:
    """检索召回的一个候选段。"""
    chunk_id: int
    recording_id: int
    segment_ids: list[int]
    text: str                   # chunk 文本
    recorded_at: datetime | None
    score: float                # 相似度分数（naive 通道 = cosine；图谱通道 = relation_counts 归一化）
    source_channel: str         # "naive" | "graph"

@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """双通道检索结果。"""
    query: str
    candidates: list[CandidateSegment]
    naive_hits: int             # naive 通道命中数
    graph_hits: int             # 图谱通道命中数
    filtered_by_time: int       # 时间过滤掉的候选数

class DualChannelRetriever:
    """双通道检索 + 时间过滤。

    Args:
        bundle: AdapterBundle（使用 weak_llm for query rewrite + embed）
        vector_store: MySQLVectorStore 实例
        graph_store: NetworkXGraphStore 实例
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
    ) -> None: ...

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        top_k: int = 10,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> RetrievalResult: ...
```

#### 数据流（四阶段流水线阶段 1-2）

| 步骤 | 输入 | 处理 | 输出 |
|---|---|---|---|
| 1a. query 改写 | `query` | `weak_llm.complete()` 提取关键词（缓存） | 改写后 query + 关键词列表 |
| 1b. query embedding | 改写后 query | `bundle.embed.embed_texts([query])` | `EmbeddingResult` |
| 2. naive 通道召回 | query 向量 | `vector_store.search_chunks(tenant_id, vec, top_k)` | `list[ChunkSearchHit]`（chunk_id + cosine score） |
| 3. 图谱通道召回 | 关键词列表 | ① 关键词匹配 graph_store 中的实体名 ② `graph_store.get_neighbors(entity_id)` 取一跳邻居 ③ 按 `relation_counts` 排序反查段 | `list[CandidateSegment]`（source_channel="graph"） |
| 4. union + 去重 | naive + graph 候选 | 按 chunk_id 去重，分数取 max | 合并候选列表 |
| 5. 时间过滤 | 候选 + `time_range` | 过滤 `recorded_at` 不在范围内的候选 | 过滤后候选列表 |
| 6. 排序 | 过滤后候选 | 按 `recorded_at` 升序排列 | `RetrievalResult` |

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **naive 文本块检索** | `vector_store.search_chunks()` 暴力余弦 top-k。对应 `vectors_chunk` 表 | §3.3 ① |
| **图谱检索** | 实体 → 一跳邻居，按 `relation_counts`（图结构信号，非纯向量相似度）排序反查段。这是 VideoRAG 的核心亮点之一 | §3.3 ② |
| **视觉通道砍掉** | 不实现视频段向量检索通道（DESIGN.md §3.4 砍/留/加） | §3.3 ③, §3.4 |
| **时间过滤** | `time_range = (start, end)` 时，过滤 `recorded_at` 不在 [start, end] 的候选。这是 AudioRAG 相对 VideoRAG 的新增维度 | §5.3 |
| **union 去重** | 同一 chunk 可能被两个通道同时召回 → 按 chunk_id 去重，score 取较高值 | §3.3 阶段 2 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| embed adapter 失败 | 向上传播（无向量无法检索） |
| vector_store 查询失败 | naive 通道返回空，仅依赖图谱通道，warning 日志 |
| graph_store 查询失败 / 图为空 | 图谱通道返回空，仅依赖 naive 通道，warning 日志 |
| 关键词无实体匹配 | 图谱通道返回空，naive 通道结果仍可用 |
| 双通道均空 | 返回空 RetrievalResult，上层 rerank 决定降级回答 |
| time_range 为 None | 不做时间过滤，返回全部候选 |

---

### 4.5 rerank.py — LLM 过滤 + 精化重排

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/core/rerank.py` |
| **职责** | 对检索候选逐段 LLM as-judge 过滤 → 提取关键词 → 高精度重转写 → 精描述升级 → 生成带三级溯源的最终回答 |
| **DESIGN.md 来源** | §3.3 四阶段流水线阶段 3-4 |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class Citation:
    """最终回答中的一条引用（三级溯源链完整表达）。"""
    entity: str                 # 命中实体名
    chunk_id: int               # 溯源 1：entity → chunk
    segment_ids: list[int]      # 溯源 2：chunk → segments
    recording_id: int           # 溯源 3：segment → recording
    recorded_at: datetime | None  # 录制时间
    transcript_snippet: str     # 段级原文摘要（精化后的精描述）
    confidence: EdgeConfidence  # 关联边的置信度

@dataclass(frozen=True, slots=True)
class RerankResult:
    """精化重排 + 回答生成完整输出。"""
    answer: str                 # 最终回答文本
    citations: list[Citation]   # 三级溯源引用列表
    filtered_count: int         # LLM as-judge 过滤掉的段数
    refined_count: int          # 精化重排处理的段数

class Reranker:
    """LLM as-judge 过滤 + 精化重排 + 回答生成。

    Args:
        bundle: AdapterBundle（strong_llm for judge/answer, weak_llm for keywords）
    """

    def __init__(self, bundle: AdapterBundle) -> None: ...

    async def rerank_and_answer(
        self,
        query: str,
        candidates: Sequence[CandidateSegment],
        *,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> RerankResult: ...
```

#### 数据流（四阶段流水线阶段 3-4）

| 步骤 | 输入 | 处理 | 输出 |
|---|---|---|---|
| 1. LLM as-judge 过滤 | query + 每个候选段 text | `strong_llm.complete()` 逐段判 yes/no 是否相关 | 过滤后存活段列表 |
| 2. 关键词提取 | query | `weak_llm.complete()` 提取查询关键词（缓存） | 关键词列表 |
| 3. 精化重排 | 存活段 + 关键词 | 对存活段定向"精看"：① 提取段内关键词 ② 调 ASR 高精度重转写（mock 下返回原 transcript） ③ 把粗描述升级为查询导向的精描述 | 精化后的段描述列表 |
| 4. 回答生成 | query + 精化段描述 + 溯源链 | `strong_llm.complete()` 生成最终回答 + 引用 | `RerankResult`（answer + citations） |

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **LLM as-judge** | 逐段让 strong_llm 判断该段是否与 query 相关（输出 yes/no）。去掉向量相似但语义无关的段。缓存生效（同 query+段 重跑免费） | §3.3 阶段 3 |
| **精化重排** | 对存活段"精看"：① 关键词提取 ② **高精度重转写**替代 VideoRAG 的"重新抽帧" ③ 粗描述 → 精描述升级。M2 mock 下 ASR 重转写返回原 transcript，但接口预留 | §3.3 阶段 4 |
| **三级溯源链完整输出** | 最终回答的每条 citation 携带 `entity → chunk_id → segment_ids[] → recording_id → recorded_at → transcript_snippet` | §3.2 |
| **边置信度透传** | citation 携带关联边的 `confidence`，让用户知道这个引用是"看到的"(EXTRACTED) 还是"猜到的"(INFERRED) | §3.1, §13.5 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| LLM as-judge 失败（单段） | 该段默认保留（保守策略，宁多勿少），warning 日志 |
| 关键词提取 LLM 失败 | 降级为使用原始 query 做精化，不阻塞 |
| ASR 重转写失败 | 使用原始 transcript 作为精描述，不阻塞 |
| 回答生成 LLM 失败 | 返回 `answer="（生成失败）"` + citations 仍返回（溯源信息不丢） |
| candidates 为空 | 返回空 RerankResult，answer="未找到相关录音片段" |

---

### 4.6 mysql_vector.py — 暴力余弦向量存储

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/storage/mysql_vector.py` |
| **职责** | entity/chunk 向量的 CRUD + 暴力余弦 top-k 检索 + float32↔BLOB 序列化 |
| **DESIGN.md 来源** | §7.4 Phase 1 全 MySQL 暴力余弦 |

#### 公共 API

```python
@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """向量检索命中结果。"""
    id: str | int               # entity_id (str) 或 chunk_id (int)
    score: float                # cosine 相似度 [-1, 1]

class MySQLVectorStore:
    """Phase 1 暴力余弦向量存储。

    向量以 float32 BLOB 存储在 MySQL（vectors_entity / vectors_chunk 表）。
    检索时全表扫描计算余弦相似度，O(N)，适合 < 10 万向量。

    Args:
        session_factory: SQLAlchemy async_sessionmaker
        dim: 向量维度（默认 1024，bge-m3）
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dim: int = 1024,
    ) -> None: ...

    # --- Entity vectors ---
    async def upsert_entity_vector(
        self, tenant_id: str, entity_id: str, embedding: tuple[float, ...]
    ) -> None: ...

    async def search_entities(
        self, tenant_id: str, query_vec: tuple[float, ...], *, top_k: int = 10
    ) -> list[VectorSearchHit]: ...

    # --- Chunk vectors ---
    async def upsert_chunk_vector(
        self, tenant_id: str, chunk_id: int, embedding: tuple[float, ...]
    ) -> None: ...

    async def search_chunks(
        self, tenant_id: str, query_vec: tuple[float, ...], *, top_k: int = 10
    ) -> list[VectorSearchHit]: ...

    # --- Utility ---
    @staticmethod
    def _serialize(vec: tuple[float, ...]) -> bytes:
        """float32 tuple → 4096-byte BLOB (numpy float32 little-endian)."""

    @staticmethod
    def _deserialize(blob: bytes) -> tuple[float, ...]:
        """4096-byte BLOB → float32 tuple."""
```

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **暴力余弦** | 全表 `SELECT` 出该 tenant 所有向量 → Python/numpy 计算 cosine → argsort top-k。无 ANN 索引，O(N)。与 NanoVectorDB 等价（Phase 1 决策） | §7.4 |
| **float32 BLOB 序列化** | `tuple[float, ...]` → `numpy.float32` array → `.tobytes()` → BLOB。反序列化反之。1024 dim × 4 bytes = 4096 bytes/行 | §7.4 |
| **租户隔离** | 所有查询带 `WHERE tenant_id = ?`，与 ORM 模型一致 | §14.2 |
| **幂等 upsert** | entity 向量按 `(tenant_id, entity_id)` 去重 upsert；chunk 向量按 `UNIQUE(tenant_id, chunk_id)` 去重 upsert | §7.5 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| MySQL 连接失败 | 向上传播 RuntimeError |
| 向量维度不匹配 | `ValueError`（dim != 1024） |
| 表为空（无向量） | 返回空列表，不抛异常 |
| BLOB 反序列化失败（数据损坏） | 跳过该行，warning 日志，不阻塞检索 |

---

### 4.7 graph_networkx.py — NetworkX 图存储

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/storage/graph_networkx.py` |
| **职责** | NetworkX 图的 CRUD + GraphML 持久化 + 邻居查询 + relation_counts 查询 |
| **DESIGN.md 来源** | §7.2 working_dir 布局；§3.1 图谱检索 |

#### 公共 API

```python
class NetworkXGraphStore:
    """NetworkX 知识图谱存储（GraphML 文件持久化）。

    每个 tenant 一个 GraphML 文件：working_dir/{tenant_id}/graph_chunk_entity_relation.graphml。
    内存中维护 NetworkX.DiGraph，生命周期节点调 save() flush 到磁盘。

    Args:
        working_dir: working_dir 根路径
        tenant_id: 租户 ID（决定 GraphML 文件路径）
    """

    def __init__(self, working_dir: Path, *, tenant_id: str = "default") -> None: ...

    # --- Node CRUD ---
    async def upsert_node(self, node: GraphNode) -> None: ...

    async def get_node(self, entity_id: str) -> GraphNode | None: ...

    async def get_all_nodes(self) -> list[GraphNode]: ...

    # --- Edge CRUD ---
    async def upsert_edge(self, edge: GraphEdge) -> None: ...

    async def get_edges(self, entity_id: str) -> list[GraphEdge]: ...

    # --- Graph queries ---
    async def get_neighbors(
        self, entity_id: str, *, max_hops: int = 1
    ) -> list[GraphNode]: ...

    async def get_relation_counts(self, entity_id: str) -> dict[str, int]:
        """返回 {relation_type: count}，用于图谱检索排序。"""

    async def get_node_degree(self, entity_id: str) -> int: ...

    # --- Persistence ---
    async def save(self) -> None:
        """flush 内存图到 GraphML 文件。"""

    async def load(self) -> None:
        """从 GraphML 文件加载图到内存。"""

    async def has_graph(self) -> bool:
        """GraphML 文件是否存在且非空。"""
```

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **GraphML 持久化** | `nx.write_graphml()` / `nx.read_graphml()`。节点属性：entity_id, name, type, description, source_ids, degree。边属性：relation, weight, confidence, confidence_score, source_ids | §7.2 |
| **relation_counts 排序** | 图谱检索时 `get_relation_counts(entity_id)` 返回该实体的各关系类型出现次数，用于排序反查段（图结构信号，非纯向量相似度） | §3.3 ② |
| **内存攒 + checkpoint flush** | 不是每次写都落盘，而是 `save()` 在生命周期节点（图谱建完 / 查询完）统一 flush | §7.2 |
| **租户隔离** | 每个 tenant 独立 GraphML 文件：`working_dir/{tenant_id}/graph_chunk_entity_relation.graphml` | §14.2 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| GraphML 文件不存在 | `load()` 初始化空图，不抛异常 |
| GraphML 文件损坏 | `load()` 初始化空图 + warning 日志（不阻塞，从 extraction 重建） |
| 节点不存在 | `get_node()` 返回 None，`get_neighbors()` 返回空列表 |
| save 失败（磁盘满 / 权限） | 向上传播，内存图保留（数据不丢） |

---

### 4.8 file_index.py — working_dir JSON 文件索引

| 属性 | 值 |
|---|---|
| **文件路径** | `backend/audio_graphy/storage/file_index.py` |
| **职责** | working_dir 下各 JSON 文件的读写 + LLM 响应缓存 + checkpoint flush 机制 |
| **DESIGN.md 来源** | §7.2 working_dir 布局与落盘机制；§7.3 LLM 响应缓存机制 |

#### 公共 API

```python
class FileIndex:
    """working_dir JSON 文件索引（VideoRAG 风格）。

    管理 working_dir/{tenant_id}/ 下的 JSON KV 文件：
    - kv_store_video_segments.json  — 段级原文（transcript/time/recorded_at/speaker）
    - kv_store_text_chunks.json     — chunk + 溯源
    - kv_store_llm_response_cache.json — LLM 响应缓存（省 token）
    - kv_store_video_path.json      — recording → 路径

    模型：内存攒着 → checkpoint 整体写盘（非每次写都落盘）。

    Args:
        working_dir: working_dir 根路径
        tenant_id: 租户 ID
    """

    def __init__(self, working_dir: Path, *, tenant_id: str = "default") -> None: ...

    # --- Generic KV ---
    async def get(self, store_name: str, key: str) -> Any | None: ...

    async def set(self, store_name: str, key: str, value: Any) -> None: ...

    async def get_all(self, store_name: str) -> dict[str, Any]: ...

    async def delete(self, store_name: str, key: str) -> bool: ...

    # --- LLM cache specific ---
    async def get_llm_cache(self, cache_key: str) -> str | None: ...

    async def set_llm_cache(self, cache_key: str, response_text: str) -> None: ...

    async def llm_cache_hit(self, cache_key: str) -> bool: ...

    # --- Persistence ---
    async def flush(self) -> None:
        """将所有内存中的 KV store 写盘（checkpoint）。"""

    async def load(self) -> None:
        """从磁盘加载所有 KV store 到内存。"""

    @property
    def working_path(self) -> Path:
        """working_dir/{tenant_id}/ 实际路径。"""
```

#### 关键算法点

| 算法点 | 描述 | DESIGN.md |
|---|---|---|
| **固定 working_dir** | 必须固定 `working_dir` 才能跨次复用索引。config.py 的 `working_dir` validator 已确保目录存在 | §7.2 gotcha |
| **LLM 响应缓存** | key = `compute_args_hash(model, messages) = MD5(str((model, messages)))`。命中 → 直接返回上次输出（不调 API、0 token）；未命中 → 调 API → 存回 → flush。对应 `kv_store_llm_response_cache.json` | §7.3 |
| **checkpoint flush** | 不是每次 set 都写盘，而是 `flush()` 在生命周期节点统一写盘。模型：内存攒着 → checkpoint 整体写盘 | §7.2 |
| **租户隔离** | 每个 tenant 独立子目录：`working_dir/{tenant_id}/` | §14.2 |

#### 错误处理

| 异常场景 | 处理策略 |
|---|---|
| JSON 文件不存在 | `load()` 初始化空 dict，不抛异常 |
| JSON 解析失败 | `load()` 初始化空 dict + warning 日志（损坏的缓存丢弃，不阻塞） |
| flush 失败（磁盘满 / 权限） | 向上传播，内存数据保留 |
| store_name 不存在 | `get()` 返回 None，`set()` 自动创建 |

---

## 5. 数据流规格 (End-to-End Data Flow)

### 5.1 索引数据流 (Indexing Data Flow)

> 对应 DESIGN.md §3.1-3.2 + §12.3 索引时序

```
recording (status=queued, pipeline_state=pending)
  │
  ▼
[1] chunker.process_recording(recording_id, audio_path, recorded_at)
  │  ├─ bundle.vad.segment(audio_path) → VADSegment[]
  │  ├─ bundle.asr.transcribe(seg) → ASRResult  (per segment)
  │  ├─ token_budget 打包 → ChunkRecord[] (segment_ids[] 溯源)
  │  └─ 写 MySQL: segments 表 + chunks 表
  │     写 file_index: kv_store_video_segments.json + kv_store_text_chunks.json
  │  pipeline_state → chunking
  │
  ▼
[2] embed chunks: bundle.embed.embed_texts(chunk_texts)
  │  └─ 写 MySQL: vectors_chunk 表 (via mysql_vector.upsert_chunk_vector)
  │  pipeline_state → embedding
  │
  ▼
[3] extractor.extract_from_chunks(chunks)
  │  ├─ bundle.strong_llm.complete() — GraphRAG 分隔符协议 prompt (cached)
  │  ├─ Gleaning 补抽 1 轮
  │  ├─ 正则解析 → ExtractedEntity[] + ExtractedRelation[]
  │  └─ 输出 ExtractionResult[] (per chunk)
  │  pipeline_state → extraction
  │
  ▼
[4] graph.build_from_extractions(extractions)
  │  ├─ 跨 chunk 合并：实体类型多数投票 + 描述去重拼接 + 边权重累加
  │  ├─ 边置信度打标：EXTRACTED / INFERRED / AMBIGUOUS
  │  ├─ embed entities: bundle.embed.embed_texts(entity_names+descriptions)
  │  │   └─ 写 MySQL: vectors_entity 表 (via mysql_vector.upsert_entity_vector)
  │  ├─ 写 graph_store: NetworkXGraphStore.upsert_node/edge → save() GraphML
  │  └─ 输出 GraphSnapshot
  │  pipeline_state → graph_merge
  │
  ▼
[5] file_index.flush()  — checkpoint 落盘
  │  pipeline_state → done
  │  recording.status → indexed, indexed_at = now()
```

| 步骤 | 输入 | 输出 | MySQL 表 | file_index / GraphML |
|---|---|---|---|---|
| 1. chunker | audio_path, recorded_at | SegmentRecord[], ChunkRecord[] | segments, chunks | kv_store_video_segments.json, kv_store_text_chunks.json |
| 2. embed chunks | chunk texts | EmbeddingResult[] | vectors_chunk | — |
| 3. extractor | chunks (id, text, recording_id) | ExtractionResult[] | — | kv_store_llm_response_cache.json (LLM cache) |
| 4. graph | ExtractionResult[] | GraphSnapshot | vectors_entity | graph_chunk_entity_relation.graphml |
| 5. flush | — | — | — | all JSON files flushed |

### 5.2 查询数据流 (Query Data Flow)

> 对应 DESIGN.md §3.3 + §12.3 查询时序

```
query ("7月1-15日哪些接待提到了CS75 Plus的金融政策?")
  │
  ▼
[1] retrieval.retrieve(query, time_range=(2026-07-01, 2026-07-15))
  │  ├─ weak_llm: query 改写 + 关键词提取 (cached)
  │  ├─ embed: query → 向量
  │  ├─ naive 通道: mysql_vector.search_chunks(vec, top_k) → ChunkSearchHit[]
  │  ├─ graph 通道: 关键词匹配实体 → graph_store.get_neighbors() → relation_counts 排序 → 反查段
  │  ├─ union + 去重 (by chunk_id)
  │  ├─ 时间过滤: recorded_at ∈ [2026-07-01, 2026-07-15]
  │  └─ 按 recorded_at 排序
  │  输出: RetrievalResult (CandidateSegment[])
  │
  ▼
[2] rerank.rerank_and_answer(query, candidates, time_range)
  │  ├─ LLM as-judge: strong_llm 逐段判 yes/no → 过滤无关段
  │  ├─ 关键词提取: weak_llm (cached)
  │  ├─ 精化重排: ASR 高精度重转写 (mock=原 transcript) + 精描述升级
  │  ├─ 回答生成: strong_llm 生成 answer + citations
  │  └─ 三级溯源链: entity → chunk_id → segment_ids[] → recording_id → recorded_at → transcript
  │  输出: RerankResult (answer + citations[])
  │
  ▼
返回: { answer, citations[], filtered_count, refined_count }
```

| 步骤 | 输入 | 处理 | 输出 |
|---|---|---|---|
| 1. retrieval | query, time_range | 双通道召回 + 时间过滤 + 排序 | RetrievalResult (CandidateSegment[]) |
| 2. rerank | query, candidates | LLM 过滤 + 精化 + 回答生成 | RerankResult (answer + citations[]) |

---

## 6. 验收标准 (Acceptance Criteria)

> 全部可测试、可量化。以下每条对应一个或一组测试用例。

### 6.1 端到端索引链路 (E2E Indexing)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-1 | 1 条录音 → VAD → ASR → chunks → entities → graph 全链路跑通 | `tests/core/test_e2e_index.py`：创建 recording → chunker → extractor → graph | GraphSnapshot 非空，nodes ≥ 1，edges ≥ 1 |
| AC-2 | segments 表写入正确 | 查 MySQL segments 表 | 行数 = VAD 分段数，idx 连续，transcript 非空 |
| AC-3 | chunks 表写入正确 | 查 MySQL chunks 表 | segment_ids JSON 非空，token_n > 0，content_hash 唯一 |
| AC-4 | vectors_chunk 表写入正确 | 查 MySQL vectors_chunk 表 | 行数 = chunks 数，embedding BLOB 长度 = 4096 bytes |
| AC-5 | vectors_entity 表写入正确 | 查 MySQL vectors_entity 表 | 行数 = GraphSnapshot.nodes 数，embedding BLOB 长度 = 4096 bytes |
| AC-6 | GraphML 文件生成 | 检查 `working_dir/{tenant}/graph_chunk_entity_relation.graphml` | 文件存在且可被 `nx.read_graphml()` 加载 |
| AC-7 | file_index JSON 生成 | 检查 `kv_store_video_segments.json` / `kv_store_text_chunks.json` | 文件存在且 JSON 可解析 |
| AC-8 | pipeline_state 正确流转 | 查 recordings 表 | pipeline_state = `done`，status = `indexed`，indexed_at 非空 |

### 6.2 端到端查询链路 (E2E Query)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-9 | 1 个问题 → 双通道检索 → LLM 过滤 → 精化 → 回答 + 溯源 | `tests/core/test_e2e_query.py`：先索引 → 再查询 | RerankResult.answer 非空，citations 非空 |
| AC-10 | naive 通道召回 | Mock 向量库有数据 | RetrievalResult.naive_hits ≥ 1 |
| AC-11 | 图谱通道召回 | 图谱有节点 | RetrievalResult.graph_hits ≥ 1（当 query 关键词匹配实体名时） |
| AC-12 | union 去重正确 | 两通道有重叠 chunk_id | 候选列表无重复 chunk_id |
| AC-13 | LLM as-judge 过滤生效 | 构造无关段 | filtered_count ≥ 1（至少过滤掉一条） |

### 6.3 三级溯源链完整性 (Provenance Chain)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-14 | entity → source_id → chunk | 检查 GraphNode.source_ids | 每个 entity 的 source_ids 非空，指向有效 chunk_id |
| AC-15 | chunk → segment_ids → segment | 检查 ChunkRecord.segment_ids | 每个 chunk 的 segment_ids 非空，指向有效 segment |
| AC-16 | segment → recording → recorded_at | 检查 Citation | citation 携带 recording_id + recorded_at + transcript_snippet |
| AC-17 | 溯源链可反查 | 从 citation 逐级反查到原始段 transcript | 反查路径完整无断链 |

### 6.4 边置信度标签 (Edge Confidence)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-18 | EXTRACTED 标签正确 | 首轮抽取的关系 | confidence = `EXTRACTED`，confidence_score = 1.0 |
| AC-19 | INFERRED 标签正确 | Gleaning 补抽的关系 | confidence = `INFERRED`，0.0 < score < 1.0 |
| AC-20 | 边置信度持久化 | 读取 GraphML 文件 | 边属性包含 `confidence` 字段，值为三者之一 |

### 6.5 时间过滤 (Time Filtering)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-21 | time_range 过滤生效 | 索引多条不同 recorded_at 的录音，查询时限定范围 | filtered_by_time ≥ 1，结果中 recorded_at 均在范围内 |
| AC-22 | time_range = None 不过滤 | 不传 time_range | 返回全部候选，filtered_by_time = 0 |

### 6.6 LLM 缓存 (LLM Cache)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-23 | 同 prompt 重跑命中缓存 | 同一 chunk 跑两次 extractor | 第二次 `LLMResponse.cached = True`，call_count 不增 |
| AC-24 | 缓存持久化 | 跑索引 → flush → 重新 load → 再跑同 chunk | 命中缓存（file_index LLM cache） |

### 6.7 开源就绪 (Open-Source Readiness)

| ID | 验收项 | 测试方法 | 通过条件 |
|---|---|---|---|
| AC-25 | pytest 全过 | `pytest` 一条命令 | 0 failed（M1 的 190 + M2 新增全部通过） |
| AC-26 | 覆盖率 ≥ 85% | `pytest --cov=audio_graphy.core --cov=audio_graphy.storage` | ≥ 85% |
| AC-27 | ruff 全过 | `ruff check && ruff format --check` | 0 errors |
| AC-28 | mypy strict 全过 | `mypy audio_graphy/core audio_graphy/storage` | 0 errors |
| AC-29 | docstring 覆盖 | 人工审查 | 所有公共类/函数有 docstring（英文为主） |
| AC-30 | 无硬编码密钥 | `grep -r "password\|secret\|api_key" core/ storage/` | 仅出现在注释/docstring 中，不在代码逻辑中 |
| AC-31 | 不依赖外部服务 | 测试用 mock adapters + testcontainers MySQL | `pytest` 不需要 vLLM/funASR/bge-m3 服务 |

---

## 7. 依赖矩阵 (Dependency Matrix)

| core 模块 | adapter 依赖 | model 依赖 | storage 依赖 | 其他 core 依赖 |
|---|---|---|---|---|
| chunker | vad, asr | Segment, Chunk | file_index | — |
| extractor | strong_llm | — | file_index (LLM cache) | — |
| graph | embed | VectorEntity | graph_networkx, mysql_vector, file_index | extractor (ExtractionResult) |
| retrieval | weak_llm, embed | VectorChunk | mysql_vector, graph_networkx, file_index | — |
| rerank | strong_llm, weak_llm, asr | — | file_index (LLM cache) | retrieval (CandidateSegment) |

| storage 模块 | 依赖 |
|---|---|
| mysql_vector | SQLAlchemy AsyncSession, numpy, models.VectorEntity, models.VectorChunk |
| graph_networkx | networkx, pathlib.Path |
| file_index | pathlib.Path, json |

---

## 8. 待确认问题 (Open Questions)

| # | 问题 | 影响模块 | 默认假设（如不确认则采用） |
|---|---|---|---|
| Q1 | **token 计数方式**：中文 token 按字符数 / 2 近似，还是用 tiktoken 精确计数？ | chunker | 默认按字符数 / 2 近似（mock 下足够，Phase 2 接真模型再换 tiktoken） |
| Q2 | **Gleaning 补抽 prompt 模板**：DESIGN.md 只说"问 LLM 是否遗漏"，具体 prompt 措辞需要架构师定还是 PM 定？ | extractor | PM 提供初版模板，架构师审校 |
| Q3 | **AMBIGUOUS 判定阈值**：实体归一后碰撞到什么程度标 AMBIGUOUS？编辑距离 ≤ 2 都合并？还是看 type 是否冲突？ | graph | 默认：归一后同名但 type 不同 → AMBIGUOUS；编辑距离聚类 ≤ 2 合并（P1 可调） |
| Q4 | **图谱检索反查段机制**：entity → neighbors 后如何反查到具体 chunk/segment？是通过 entity.source_ids 还是边上 source_ids？ | retrieval | 默认通过 GraphNode.source_ids 反查 chunk_id → chunk.segment_ids → segment |
| Q5 | **高精度重转写在 mock 下的行为**：rerank 调 ASR 重转写，mock ASR 返回什么？原 transcript？还是加噪声？ | rerank | 默认返回原 transcript（mock 下不做真实重转写，Phase 2 接真 ASR 再实现） |
| Q6 | **LLM 缓存与 MockLLMAdapter 的关系**：MockLLMAdapter 已有 `_cache` dict，file_index 也有 LLM cache。两者如何协作？是 mock 用自己的 cache 还是走 file_index？ | extractor, rerank, file_index | 默认：core 模块通过 `cache_key` 参数让 adapter 自行缓存；file_index 的 LLM cache 作为持久化层（跨进程复用），mock adapter 的 `_cache` 是进程内缓存。两者互补。 |
| Q7 | **跨录音合并的时机**：graph.build_from_extractions 是每次索引一条录音就合并进全局图，还是攒多条再批量合并？ | graph | 默认：每次索引一条录音就合并进全局图（增量合并，DESIGN.md §3.1 跨录音机制）。幂等保证重跑不重复。 |
| Q8 | **测试数据库策略**：core 模块的集成测试用 testcontainers MySQL 还是 SQLite in-memory？ | 测试 | 默认：testcontainers MySQL 8（与生产一致，§开源就绪约束）。SQLite 不支持 JSON 列类型和 BLOB 操作可能有差异。 |
| Q9 | **GraphML vs NetworkX JSON**：DESIGN.md §7.2 说 GraphML，但 §13.6 提到 Graphify 用 graph.json。M2 用哪个格式？ | graph_networkx | 默认：GraphML（DESIGN.md §7.2 权威），前端通过 API 序列化为 JSON |
| Q10 | **prompt 模板加载方式**：从 `prompts/entity_zh.md` 文件读取，还是硬编码在 Python 常量中？ | extractor | 默认：从文件读取（P1-1），支持 prompt 版本切换。M2 先用文件读取 + 缓存。 |

---

## 附录 A · 数据结构速查 (Data Structure Quick Reference)

> 所有 dataclass 定义在 `core/` 各模块中，storage 模块复用 core 的 dataclass。

```
SegmentRecord          ← chunker 产出
  idx, start_sec, end_sec, transcript, speaker, vad_conf

ChunkRecord            ← chunker 产出
  segment_ids[], text, token_n, content_hash

ChunkerOutput          ← chunker 产出
  recording_id, segments[], chunks[]

ExtractedEntity        ← extractor 产出
  name, type, description, chunk_id, recording_id

ExtractedRelation      ← extractor 产出
  source_name, target_name, relation, description, weight, confidence, chunk_id, recording_id

ExtractionResult       ← extractor 产出
  chunk_id, recording_id, entities[], relations[], parse_success, gleaning_rounds

GraphNode              ← graph 产出（graph_networkx 存储）
  entity_id, name, type, description, source_ids[], recording_ids[], degree

GraphEdge              ← graph 产出（graph_networkx 存储）
  source, target, relation, weight, confidence, confidence_score, source_ids[]

GraphSnapshot          ← graph 产出
  nodes[], edges[], total_entities, total_relations, cross_recording_entities

CandidateSegment       ← retrieval 产出
  chunk_id, recording_id, segment_ids[], text, recorded_at, score, source_channel

RetrievalResult        ← retrieval 产出
  query, candidates[], naive_hits, graph_hits, filtered_by_time

Citation               ← rerank 产出（三级溯源链）
  entity, chunk_id, segment_ids[], recording_id, recorded_at, transcript_snippet, confidence

RerankResult           ← rerank 产出
  answer, citations[], filtered_count, refined_count

VectorSearchHit        ← mysql_vector 产出
  id (str|int), score
```

---

## 附录 B · EdgeConfidence 判定规则 (Edge Confidence Rules)

| 标签 | 判定条件 | confidence_score | 场景示例 |
|---|---|---|---|
| `EXTRACTED` | 首轮抽取、transcript 中明确存在的关系 | 1.0 | "坐席推荐了CS75 Plus" → (坐席)-[推荐]→(CS75 Plus) |
| `INFERRED` | Gleaning 补抽、或跨段合并推断的关系 | 0.0 < score < 1.0（= weight 归一化） | Gleaning 补抽出"客户对比了竞品" → (客户)-[对比]→(竞品) |
| `AMBIGUOUS` | 实体归一后碰撞（同名不同实体被合并） | None | 两个不同"客户"被合并到同一节点 → 关联边标 AMBIGUOUS 待人工审核 |

---

**文档结束 (End of Document)**

> 本 PRD 是 M2 工程实现的唯一需求权威源。架构师设计文档（M2-S2）和工程师代码实现（M2-S3）必须以本文档为准。任何偏离需经 PM 确认后更新本文档。
