# 标签治理闭环

AudioGraphy 的权威标签主体是 `Reception` 与 `DialogueUnit`。旧的
Recording 级标签只保留兼容读取，不能被复制到接待内的所有对话单元。

## 闭环总览

```mermaid
flowchart LR
    A["多段录音合并"] --> B["对话切分"]
    B --> C["发布中的 Schema"]
    C --> D["版本化 Tagger"]
    D --> E["异步抽取任务"]
    E --> F["不可变标签事实"]
    F --> G["人工复核"]
    G --> H["冻结黄金集"]
    H --> I["规则、Prompt、阈值候选"]
    I --> J["隐藏集离线评估"]
    J --> K["Shadow / 5% / 25%"]
    K --> L["管理员批准生产"]
    L --> M["监控与自动回滚"]
    M --> G
```

闭环不是一组互相独立的页面。每一步都以不可变版本或可恢复任务为边界，
并通过 `tenant_id`、输入哈希、事实谱系和审计事件连接。

## 领域不变量

1. `tag_assignment_facts` 和 `tag_review_decisions` 只追加；数据库触发器和
   ORM 事件同时拒绝 UPDATE/DELETE。
2. `tag_assignment_current` 只保存当前事实指针。写事实、Schema 校验和
   current 切换在同一事务完成；失败时旧 current 保持可见。
3. 必须证据的定义若没有有效 Segment 证据，不产生标签事实。
4. 业务 `input_hash` 覆盖规范化转写、Segment 版本与时间范围、
   DialogueUnit 版本、Schema、规则、Prompt、阈值和模型版本；它用于事实
   谱系与本地策略重放，不直接充当 LLM 生成缓存键。生成缓存使用独立的
   `llm-recipe-v2`，避免阈值、融合或部署状态变化误触发 Provider 调用。
5. 对话拆分、合并、重转写或重新切分仅使受影响主体的 current 失效，并
   创建定向重算任务。已删除的 DialogueUnit 会被记录为失效对象，但不会
   进入重算范围。
6. 自动优化只创建候选 `tagger_version`，不能直接改变生产路由。
7. 所有列表、详情、任务、事实和部署操作都从登录上下文读取租户，不接受
   请求体覆盖租户。
8. 盲审、抽样概率、真值层级、复核轮次和仲裁身份均由服务端派生；浏览器
   只能提交复核原因、主体和人工判断，不能自证为 T2/T3 或代表性样本。

## 状态机

| 资源 | 状态 |
|---|---|
| Job | `queued → running → retry_wait → completed / failed / cancelled` |
| Review | `pending → claimed → resolved / skipped` |
| Tagger | `draft → validating → evaluating → rejected / qualified` |
| Deployment | `shadow → canary_5 → canary_25 → awaiting_admin → production → rolled_back / retired` |

任务由 MySQL lease 驱动。Worker 使用 `SELECT … FOR UPDATE SKIP LOCKED`
认领任务、周期心跳、CAS 更新 revision，并回收 stale lease。创建接口支持
`Idempotency-Key`，重复提交不能生成重复任务或重复事实。

## 抽取输入与输出

统一 `TagExtractor` 从发布的 Schema 定义读取标签键、值域、主体范围、
适用场景、证据要求和默认阈值：

- rule：执行受约束 JSON DSL，只允许字符串匹配、优先级和置信度等数据项；
- LLM：使用 `tagger_version.prompt_content` 的真实内容；
- hybrid：规则和模型使用相同输入快照，冲突进入复核。

抽取输出先通过定义、值域、阈值、主体、场景和证据所有权校验，再写入事实。
事实的溯源链至少包含：

`fact → extraction_run → job → input_hash → schema_version → tagger_version → model/deployment → evidence Segment`

人工纠正生成 `source=manual` 的新事实；人工驳回生成 tombstone 事实。历史
候选事实仍可审计，不会被物理删除。

## Token 质量守恒

LLM 输入被拆成三个边界，任何本地策略都不能混入生成缓存键：

1. 稳定系统指令：只描述任务、结构化输出和证据约束；
2. 语义 Prompt：来自不可变 `TaggerVersion.prompt_content`；
3. 传输负载：只发送本次未解决标签的最小定义，以及
   `s0/s1/... → speaker/time/text` 的紧凑 Segment 映射。

输出通过 Provider 原生 strict JSON Schema 约束。Adapter 必须显式声明结构化
输出能力，不能静默丢弃 Schema；不支持时只能进入受控 JSON mode。截断、结构
错误和证据错误分别记录，格式修复最多一次，并且只修复原始输出，不重发全文。
模型只能返回本次请求的短 Segment ID 与目标标签；越界标签、未知证据或非法
结果整批拒绝，不进入缓存，也不切换 current。

执行器按标签逐级缩小调用范围：

- 置信度 `≥ 0.95` 的非关键规则可直接产出；关键标签不能由规则单独短路；
- Weak 只处理 unresolved 标签，Strong 只复核关键标签、冲突、阈值边缘、
  缺证据或解析不确定项；
- Strong 只接收 Weak 候选、升级标签定义与相关证据，非关键升级率最多 20%；
- Weak/Strong 指向同一 model epoch 时折叠成一次调用；
- 输出预算按 `ceil256(128 + 96 × 标签数)` 取 256–2048 档位，并分别计算
  Weak 与 Strong；
- 单主体保留输出与 10% 安全窗口后，模型输入硬上限为 12,000 Token；超限
  的 Reception 先聚合同租户、同 Schema 的 DialogueUnit current/cached
  事实及每单元最多两个真实证据 Segment；聚合仍超限才按 Segment 确定性
  分块并本地合并。聚合 checksum 进入业务 input hash，DU/fact 引用进入
  provenance，事实变化不会复用旧结果且可随 DSAR 安全失效。

`target_tag_keys` 从 Job scope 一直贯穿定义过滤、生成 Schema、输入哈希、
模型校验和事实持久化。过滤后没有标签时执行零调用。Canary 的 5%/25% 是实际
候选调用比例，而不只是发布比例；Shadow 默认只生成稳定散列命中的 10% 样本，
可信质量样本达到门槛后写入 `sampling_complete_at`，停止额外候选调用但继续
观测已有样本。

生成缓存使用 `llm-recipe-v2`：

- generation hash 只包含租户/权限边界、实际消息和 Schema、Provider/model
  epoch、生成参数及 parser/postprocessor 版本；
- 阈值、融合、部署状态等进入 policy hash，本地变化只重放后处理；
- provenance 作为引用集合保存，不参与 generation hash，同时保持租户隔离和
  DSAR 失效；
- 迁移依次使用 `shadow → dual_read → v2`，先观测 v1/v2 命中差异，再双读
  v1、写 v2，最后只读写 v2。

用量账本以成功的 `provider_attempt` 为唯一实际消费口径，并通过唯一约束防止
Worker 重跑或日志聚合重复计费。缓存命中的实际 Provider Token 为零，历史
usage 只记为 `counterfactual_saved_tokens`。输入、输出、cached prefill、
Weak/Strong、重试、截断以及 `unknown_billed` 分开记录，并关联 logical
request、attempt、Tagger、Deployment、Evaluation、Optimization/Trial、
价格版本和 `cost_microunits`。预算在调用前原子预留、调用后按实际结算；
不足时标记 `budget_exhausted`，不发布不完整 current。

预算启用后的前 7 天（且完整成功样本少于 100 条）只告警并采集
`tenant + job_type + purpose` 基线，不阻断调用；基线成熟后按最近完整样本的
nearest-rank P99 × 1.2 生成 Token、调用数、真实成本和墙钟时间四维默认硬预算，
调用方显式预算始终优先。优化与隐藏集评估要求持久用量账本：
Provider attempt 写账失败、观察器缺失或价格/计费状态不完整都会失败关闭，
候选不得晋级。

## 六阶段 Harness 与输入冻结

每个 Tagger 版本都携带一份经过边界校验的六阶段 Harness，而不是在运行时
临时拼装 Prompt：

| 阶段 | 版本化内容 |
|---|---|
| Context | 上下文窗口、场景画像、示例数量与选择策略 |
| Tools | 规则、弱模型、强模型及 critic 能力 |
| Generation | Prompt、采样参数、结构化输出与 token 上限 |
| Orchestration | rule-only、LLM-only、融合或 weak→strong critic 路由 |
| Memory | 只读金标示例检索与隔离范围 |
| Output | 阈值偏移、融合策略、证据约束和 review fallback |

旧版 Harness 配置只在读取边界做确定性转译，进入执行器后始终使用同一份规范
结构。每次执行会保存解析后的 Harness、场景画像、逐阶段 trace、延迟、
token 和成本，因此任一候选都能按原输入重放。

离线评估与候选比较读取冻结的 `input_snapshot`，不会回退到此后被人工编辑
的在线 DialogueUnit 或 Segment。快照显式携带 `subject_type`；Reception
标签与 DialogueUnit 标签使用各自定义域和指标分母，禁止跨主体复用样本。

## 复核与黄金集

以下情况自动或按洞察筛选创建复核任务：

- 规则与模型冲突；
- 必填标签缺失；
- 低置信度；
- 关键合规标签；
- 随机质检；
- 分布漂移或异常簇。

复核决定包括接受、纠正、驳回、原因码、备注和证据。真值按以下可信层级
进入学习闭环：

- T1：普通单人复核，可形成诊断反馈，不承担关键发布真值；
- T2：服务端创建的 blind audit / critical / gold 任务，复核人看不到模型
  候选、执行谱系和历史结论；
- T3：critical / gold 的两名不同复核人分别完成 T2 后，由第三名复核人
  仲裁；创建者、两个 T2 复核人和仲裁人不能复用身份。

复核工作台把“进行中队列”和“历史结果”分开：默认队列只返回待领取任务
及本人已领取任务，不会因为刷新队列而读取模型结论。领取后可进入仍然脱敏
的音频/转写工作台；退出或换班时可主动“放弃任务”，管理员也可强制释放
僵尸领取。模型输出、历史标签、统计和导出仍受盲审访问保留机制约束。

关键标签和 gold matrix 的两个 T2 轮次不能直接冻结。冻结时按
`subject_type + subject_id + tag_key` 归并，存在认证 T3 时只选择 T3，
并把两个 T2 predecessor 写入质量谱系。黄金集同时校验 Schema、证据、
输入快照、抽取运行、Harness 执行和部署来源；同一 subject 的所有标签格
必须来自同一次服务端选定的规范抽取快照，不能把历史 current fact 与新输入
拼成伪样本。随后按 Reception 稳定散列为
60% train、20% validation、20% hidden holdout，同一接待绝不跨分片。

## 优化、评估与发布

V2 优化器只读取训练集，在验证集搜索 Prompt、路由、上下文、输出预算、
声明式规则和阈值候选。会改变 Provider 输入的候选必须真实执行
`TagExtractor`；阈值和融合等纯本地策略复用相同原始生成结果重放。相同生成
配置去重执行，冷缓存成本与暖缓存节省分开报告，隐藏集只用于 winner 的一次
发布门禁。未接入 serving 的搜索维度会在候选物化前被拒绝。

优化请求只提交 cohort 意图、目标策略和搜索预算。当前 production
Harness、完整冻结金标、训练样本、真值和 Reception 范围均由服务端解析并
冻结 checksum，浏览器不能注入基线或隐藏集。创建优化运行会在同一事务中
建立持久化 `optimize` Job；Worker 认领后逐个执行有界 trial，把每个 trial
的指标、reward、胜出项和候选 Tagger 写回数据库。失败可按 lease 规则恢复，
取消会同时终止绑定任务，且取消状态不能被后续 enqueue/finalize 复活。
训练 cohort 只影响 train / validation 搜索，不能裁剪或探测 sealed holdout；
隐藏集只执行服务端全局发布就绪检查，并按隐藏样本自身的稳定内容指纹实施
一次性消费；修改 train / validation、cohort 或 baseline 不能重置预算。
不足时只返回统一的不可发布结果，不暴露标签、分片或真值状态。

每个 trial 在执行前先持久化不可变候选规格和冻结数据 checksum，再在短事务
外调用 Provider，完成后重新校验运行状态、Job lease 和 manifest 才写回。
搜索预算聚合限制 Provider Token、调用数、真实成本和墙钟时间；预算不足在
Provider 调用前失败关闭。每个 Provider trial 的预算预留也写入
`TagOptimizationRun.search_budget`；进程若在调用后、结算前崩溃，下一租约会
把未结算预留按全额消费，避免重启后重复花费；任一主体、计费或账本测量不完整
时不结算、不释放预留。基线与候选人工复核率均按 subject/tag 明细比较，旧
execution 只有总计数而没有 `review_items.tag_key` 时失败关闭。三种目标具有
明确次序：

- `quality_first`：质量门禁内先最大化 Macro-F1，再比较 Token；
- `efficiency_guarded`：质量门禁内先最小化 Provider Token，再比较质量；
- `balanced`：在 Pareto 前沿等权比较标准化质量损失与 Token 成本。

创建真实优化任务前还有一层反馈覆盖门：

- 只统计上次真实运行后的新 T2/T3、`present/absent` 且
  `training_eligible=true` 的语义反馈；
- ASR、说话人分离等上游失败不作为标签策略学习样本；
- 每轮至少 200 条新可信反馈，每个受影响的 `subject_type + tag_key`
  域至少 30 条；
- 未达门槛时只保存 `diagnostic_only` 运行与具体 blocker，不创建 trial、
  Job 或候选版本；
- Worker 每周按 ISO 周执行一次幂等覆盖检查；同租户已有运行中任务时跳过。

优化器只生成 `draft` 候选。候选使用与线上相同的 `TagExtractor` 和六阶段
Harness 执行，必须重新经过验证、仅一次解封的隐藏集评估和发布门禁；即使
trial 胜出，也不能直接修改 production 路由。结构化人工反馈会在决定事务中
物化为 feedback event、badcase 和可检索 experience，供后续复核批次和候选
生成使用，但不会直接改写 Prompt、Schema 或标签事实。

| 门禁 | 规则 |
|---|---|
| Macro-F1 | `≥ 0.80`，且相对生产版下降不超过 0.01 |
| 关键标签召回 | Wilson 置信下界 `≥ 0.95` |
| 必须证据覆盖率 | `≥ 0.98` |
| 抽取错误率 | `< 0.01` |
| 单标签 F1 | 支持数 ≥ 30 时下降不超过 0.01 |
| 冷缓存 Provider Token/subject | 相对基线下降 `≥ 20%`，配对 bootstrap 95% CI 下界 `≥ 10%` |
| 真实成本 | 相对基线下降 `≥ 15%` |
| P95 延迟 | 恶化不超过 `5%` |
| 人工复核率 | 增加不超过 `0.01` |
| Provider calls/subject | 不得上升 |

样本不足不能自动晋级。通过门禁后使用
`hash(tenant_id, reception_id, deployment_id)` 稳定分桶：

- Shadow：500 个接待或 24 小时；
- Canary 5%：200 个接待；
- Canary 25%：1000 个接待；
- 管理员批准后进入 production。

发布推进只计服务端记录的去重 subject 身份，并要求当前阶段连续、无缺口的
五分钟监控窗口。连续三个五分钟窗口错误率超过 2%、Schema/证据一致性错误、重复 current，
或关键标签复核召回跌破门禁会自动回滚。单纯分布漂移只暂停晋级并创建复核
批次。

分布漂移使用同租户、同主体、同业务输入快照的候选/基线成功运行做配对，
不依赖候选结果成为 current，因此 Shadow 阶段也能形成真实观测。系统按
`subject_type + subject_id + 业务输入指纹` 去重，逐个 `tag_key` 对标签值
（包含“缺失标签”）计算 base-2 Jensen–Shannon divergence：

- 每个标签至少 30 个有效配对样本才参与门禁；
- `JSD > 0.10` 标记为漂移；
- 输入场景画像以当前候选对比近 28 天生产基线计算 PSI，并按主体域隔离；
- 观测保留两侧分布、样本数、JSD 和受影响标签；
- 漂移只暂停晋级并建立复核批次，不能单独触发自动回滚；
- 采集期间部署 stage 或 revision 改变时丢弃旧窗口，避免把观测写入错误阶段。

漂移复核“完成”不等于允许恢复。只有该暂停观测对应的服务端复核批次全部
形成 T2/T3 definitive truth，且每一项都支持候选标签，管理员提交不少于
8 个字符的理由并通过 `If-Match` revision 校验后，系统才解除暂停；任一
驳回或纠正差异都会继续暂停，并要求回滚或生成新候选。

回滚会原子切回 baseline 路由，恢复 baseline current；无法直接恢复的范围
进入 `remediate` 定向任务。候选事实、观测和回滚事件全部保留。

## API 与前端入口

| 资源 | 作用 |
|---|---|
| `/tag-schemas` | 标签体系和不可变版本 |
| `/tagger-versions` | 规则、Prompt、模型和阈值版本 |
| `/tag-jobs` | 异步抽取、重算、评估和修复任务 |
| `/tag-reviews` | 证据复核、纠正、驳回与仲裁 |
| `/tag-gold-sets` | 黄金集创建和冻结 |
| `/tag-evaluations` | 隐藏集评估及质量门禁 |
| `/tag-deployments` | Shadow、灰度、批准、监控和回滚 |
| `/tag-evolution/overview` | 闭环健康度、真值层级和待办聚合 |
| `/tag-badcases` | 结构化反馈、误差簇与可追溯 badcase |
| `/tag-optimization-runs` | 服务端绑定基线的优化任务、trial、比较与取消 |

前端 `/tag-governance` 管理体系、版本、实验、发布和审计；
`/tag-review` 完成证据调听与人工决定；`/tag-runs/:id` 展示任务检查点、失败
子集、重试和取消。标签洞察页可以基于当前筛选直接创建复核批次、定向重算或
优化候选，并在生产版和候选版之间做质量对比。

旧 `/tags`、`/prompts` 和同步 `dialogue-tags/derive` 接口在兼容期返回
`Deprecation`、`Sunset` 与 successor `Link` 响应头。

部署在 Sites Worker + D1 的同名接口是明确标记
`is_demo=true / data_source=demo` 的确定性产品演示：它会持久化演示运行和
trial，便于验证前端交互，但不宣称执行真实模型优化。Docker Compose 中的
API + MySQL + `tag-worker` 才是执行真实冻结输入、模型 Adapter、评估与发布
门禁的生产闭环。

## 运维检查

```bash
# 迁移
cd backend
.venv/bin/alembic upgrade head

# 启动 API 与持久化 Worker
docker compose --profile mock up -d backend tag-worker
docker compose logs -f backend tag-worker

# 检查任务和发布状态
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/tag-jobs
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/tag-deployments
```

生产环境还应监控 queued age、stale reclaim、事实/current 一致性、证据
覆盖率、关键标签 Wilson LCB、按版本错误率、Provider Token/subject、
cost/subject、Strong 升级率、重试浪费、缓存层级及回滚修复任务。Token 或
cost 连续两个完整 15 分钟窗口高于基线 10% 时暂停晋级；高于 25%、预算将
耗尽或触发质量硬门禁时自动回滚。

预算信号只读取窗口内 serving run 关联的服务端 Job 账本。活跃 Job 任一硬预算
使用率达到 90% 时暂停晋级并创建或复用 `budget_guard` 盲审批次；非成功 Job
达到 100% 或带有明确 `budget_exhausted` 标记时自动回滚。成功完成的 Job 即使
恰好用满预算也只观测，不产生误回滚；客户端自报的预算指标不能控制部署。
