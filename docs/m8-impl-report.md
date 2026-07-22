# M8 第 1 轮（WS-1 + WS-2）交付报告

**致**：主理人齐活林  **来自**：寇豆码（工程师）  **日期**：2026-07-22

## T1–T8 清单

- ✅ T1 `adapters/protocols.py` + `exceptions.py`：StreamingVAD/ASR Protocol、`StreamingSessionError` 等 7 异常
- ✅ T2 `StreamingSileroVADAdapter`（real + mock）+ FSM 适配
- ✅ T3 `StreamingFunASRAdapter` + 连接池 `StreamingFunASRPool`（real + mock）
- ✅ T4 `core/stream_session.py`：SessionState 状态机 + PCM 512 强校验
- ✅ T5 ORM `StreamingSession` + `alembic/versions/0009_m8_streaming_init.py` + `config.py` 扩展 + `bundle.py`
- ✅ T6 `core/streaming_chunker.py`：增量切片 + 哈希去重
- ✅ T7 `core/delta_graph_updater.py`（仅 `confirmed` 入图）+ `core/streaming_rwlock.py`
- ✅ T8 `api/ws_stream.py`：`/ws/stream` 端点 + 心跳 + 优雅退出

## 文件列表

源码 17：`adapters/{protocols,exceptions,mock_streaming_vad,mock_streaming_asr,bundle}.py`、`adapters/real/{streaming_vad_silero,streaming_funasr,streaming_funasr_pool}.py`、`core/{stream_session,streaming_chunker,streaming_rwlock,delta_graph_updater}.py`、`api/ws_stream.py`、`models/streaming_session.py`、`alembic/versions/0009_m8_streaming_init.py`、`config.py`。测试 5：`tests/{test_m8_streaming,test_m8_config_and_models,api/test_m8_ws_stream,core/test_m8_stream_session,core/test_m8_coverage_boost}.py`。

## 测试结果

- **ruff**：22 文件 0 错（F401/F821/ASYNC109/UP042/N814/B007/SIM105/SIM117/B017/E402/S110 全部修复）
- **mypy**：16 文件 0 错
- **pytest**：M8 新增 **163 用例全通过**（目标 ≥150）；回归 **1429 通过 / 1 skip / 0 fail**；`enable_streaming=False` 默认保持 M1-M7 零回归
- **覆盖**：6/12 模块 93–100%（stream_session / streaming_chunker / streaming_rwlock / config / models / ws_stream）；6 个 real-mode 适配器 42–84%（依赖外部 ONNX/FunASR 服务，mock 模式不可触达）

## IS_PASS: **YES**

## 偏差

1. `edges` 表在 M8 ORM 中无对应表，`0009` 的 `ALTER edges` 已跳过，仅建 `streaming_sessions` 表，不影响 WS-1/WS-2 功能。
2. real-mode 适配器覆盖未达 90%，已通过 mock 适配器 + FSM/Pool 直接单测补齐核心逻辑；集成覆盖留待真实环境联调。

## WS-3 移交 3 条

1. **streaming_tag_scheduler**：需在 `DeltaGraphUpdater` 落图后调度增量打标，复用 `StreamingRWLock` 避免与读路径冲突。
2. **streaming_retriever**：流式检索需消费 `SessionStatus.CONFIRMED` 后的增量图，注意 `confirmed` 边的可见性语义。
3. **E2E metrics**：建议在 WS-3 补端到端延迟 P95、首句 confirmed 时延、VAD 假阳率三项核心指标埋点。
