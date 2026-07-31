# AudioGraphy 模型部署指南

本指南描述当前 `docker-compose.yml` 的四种互斥模型拓扑，以及可叠加的
Redis 热缓存 Profile。所有检查都可以通过
`docker compose config` 完成，不需要拉取或启动大模型。

## 1. Profile 总览

| Profile | 启动的模型服务 | GPU | 适用场景 |
|---|---|---:|---|
| `mock` | 无；所有 adapter 保持 mock | 0 | 开发、CI、前端联调 |
| `models-cpu` | funASR、BGE-M3 CPU、CAM++ | 0 | CPU 验证、离线小批量处理 |
| `models-single-gpu` | CPU 服务 + vLLM strong、BGE-M3 GPU、CLAP | 1 | 单卡推理；strong/weak 逻辑共用一个 vLLM |
| `models-multi-gpu` | 单卡全部服务 + vLLM weak | 2+ | strong/weak 分卡部署 |

Profile 是互斥的。不要在同一命令同时启用 `models-cpu` 和 GPU profile，
因为 CPU/GPU BGE 服务会同时占用相同的网络别名与宿主机端口。

核心服务 `mysql`、`backend`、`frontend` 在所有拓扑中都会启动；
`adminer` 只属于 `mock` profile。

`cache-redis` 不是模型拓扑，可与上表任一 Profile 组合。MySQL 始终是 LLM
缓存持久化层；Redis 只承担可丢失的共享热缓存。

## 2. 固定镜像与安全边界

| 服务 | 镜像或固定基础镜像 |
|---|---|
| MySQL | `mysql:8.0.41` |
| Adminer | `adminer:5.3.0` |
| vLLM strong / weak | `vllm/vllm-openai:v0.7.2` |
| BGE-M3 CPU | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8.2` |
| BGE-M3 GPU | `ghcr.io/huggingface/text-embeddings-inference:cuda-1.8.2` |
| funASR | `funasr/server:1.0.5` |
| CLAP | 自建；基础镜像 `pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime` |
| CAM++ | 自建；基础镜像 `python:3.11.11-slim-bookworm` |

Compose 与两个自建模型 Dockerfile 中没有 `:latest`。CLAP/CAM++ 使用固定
UID/GID `10001` 的非 root 用户；模型服务统一启用
`no-new-privileges` 与 `cap_drop: ALL`。兼容的服务还启用了只读根文件系统，
只给 `/tmp` 和模型缓存卷写权限。

录音主密钥不写入镜像、仓库或环境变量。Compose 会先运行一次性的
`master-key-init`，在独立 `audiography_master_key` 卷中生成或校验 0600
Fernet 密钥；backend 仅以只读方式挂载该卷，并在启动时立即校验。密钥缺失或
损坏时服务失败关闭，不会退回明文录音。该卷必须与音频数据分开备份，删除它会
使既有密文不可恢复。Kubernetes/生产编排应改为外部 KMS 或 Secret 挂载同一路径，
并移除本地初始化器。

Backend 还在 ASGI 入口同时校验 `Content-Length` 和分块累计字节，默认拒绝超过
16 MiB 的 HTTP 请求体，防止合法但超大的标签快照在 Pydantic 解析前占满内存；
反向代理应设置不高于 `MAX_REQUEST_BODY_BYTES` 的同等限制。

注意：Compose 的 `deploy.resources.limits.memory` 限制的是主机内存，不是
显存。显存边界由显式 `device_ids`、vLLM 的
`--gpu-memory-utilization` 以及模型服务本身共同控制。

## 3. 快速启动

### 3.1 Mock

```bash
cp .env.example .env
docker compose --profile mock up -d
docker compose --profile mock ps
curl http://127.0.0.1:8000/health
```

`.env.example` 默认全部为 mock，不下载模型，也不需要 GPU。

### 3.2 CPU 模型

在 `.env` 至少设置：

```dotenv
ADAPTER_ASR_MODE=real
ADAPTER_EMBED_MODE=real
ADAPTER_VOICEPRINT_MODE=real
ENABLE_VOICEPRINT=true

ADAPTER_LLM_MODE=mock
ADAPTER_VAD_MODE=mock
ADAPTER_AUDIO_EMBED_MODE=mock
```

然后启动：

```bash
docker compose --profile models-cpu up -d
docker compose --profile models-cpu ps
```

CPU profile 不含 CLAP，因为 CLAP 服务在本项目中强制使用 CUDA。

### 3.3 单 GPU

单卡 profile 只启动 `vllm-strong`。后端仍保留 strong/weak 两个逻辑角色，
因此把 weak 指向同一服务，并让两个逻辑模型名都匹配 strong 的
`--served-model-name`：

```dotenv
ADAPTER_ASR_MODE=real
ADAPTER_EMBED_MODE=real
ADAPTER_LLM_MODE=real
ADAPTER_AUDIO_EMBED_MODE=real
ADAPTER_VOICEPRINT_MODE=real
ENABLE_CLAP=true
ENABLE_VOICEPRINT=true
ADAPTER_VAD_MODE=mock

OPENAI_BASE_URL_STRONG=http://vllm-strong:8000/v1
OPENAI_BASE_URL_WEAK=http://vllm-strong:8000/v1
LLM_STRONG_MODEL=qwen3.6-27b
LLM_WEAK_MODEL=qwen3.6-27b
```

```bash
docker compose --profile models-single-gpu up -d
docker compose --profile models-single-gpu ps
```

默认模型需要大显存卡。24 GB 单卡应通过 `VLLM_STRONG_MODEL` 和
`VLLM_STRONG_SERVED_NAME` 换成经过验证的小模型或量化模型，同时同步
`LLM_STRONG_MODEL` / `LLM_WEAK_MODEL`。

### 3.4 多 GPU

默认分配如下：

| 服务 | 默认宿主 GPU |
|---|---:|
| `vllm-strong` | `0` |
| `vllm-weak` | `1` |
| `bge-m3-gpu` | `0` |
| `clap-service` | `0` |

```dotenv
ADAPTER_ASR_MODE=real
ADAPTER_EMBED_MODE=real
ADAPTER_LLM_MODE=real
ADAPTER_AUDIO_EMBED_MODE=real
ADAPTER_VOICEPRINT_MODE=real
ENABLE_CLAP=true
ENABLE_VOICEPRINT=true
ADAPTER_VAD_MODE=mock

OPENAI_BASE_URL_STRONG=http://vllm-strong:8000/v1
OPENAI_BASE_URL_WEAK=http://vllm-weak:8000/v1
```

```bash
docker compose --profile models-multi-gpu up -d
docker compose --profile models-multi-gpu ps
```

可通过以下变量重新分配设备，不需要改 YAML：

```dotenv
VLLM_STRONG_GPU_ID=0
VLLM_WEAK_GPU_ID=1
BGE_M3_GPU_ID=0
CLAP_GPU_ID=0

VLLM_STRONG_GPU_MEMORY_UTILIZATION=0.72
VLLM_WEAK_GPU_MEMORY_UTILIZATION=0.82
```

每个 GPU 服务只申请一个明确的 `device_id`，不会再使用 `count: all`。

### 3.5 可选 Redis 热缓存

未设置 `REDIS_URL` 时，LLM 网关使用每进程最多 1024 项 / 32 MiB 的 TTL+LRU
本地缓存，并由 MySQL 提供重启复用和跨进程 singleflight。需要多进程共享热
缓存时，可叠加。请求内的 memo 不跨请求存活，且在 Redis 健康时不会再保留
一份重复的进程级结果缓存：

```bash
REDIS_URL=redis://redis:6379/0 \
  docker compose --profile models-multi-gpu --profile cache-redis up -d
```

Redis 启动探测失败或运行中连续三次操作失败时，网关自动降级到本地缓存；
30 秒熔断后后台探测，连续两次成功才恢复。缓存错误只产生 miss 和告警。
Compose 实例使用 128 MiB `allkeys-lru`、192 MiB 容器内存上限，并关闭
AOF/RDB。外部 Redis 应使用隔离的实例或 DB；应用不会修改全局淘汰策略，也
不会使用 `KEYS` 或 `FLUSHDB`。原始 prompt 不落 Redis/MySQL；经过结构校验的
输出先压缩，再以绑定 tenant/namespace/recipe 的 AES-256-GCM 进行认证加密。

发布相关开关位于 `.env.example`。精确 MySQL 缓存默认开启；语义缓存、候选
批判断、hybrid 规则短路和自适应 gleaning 默认关闭，应在对应金标质量门禁
通过后逐项启用。
`ENABLE_LLM_HOT_CACHE` 与 `ENABLE_LLM_PERSISTENT_CACHE` 可独立回退。只关闭
持久层时，无 provenance 的精确结果继续使用热缓存；带 provenance 的请求
强制绕过 hot-only 模式，确保不会绕开 DSAR 所需的 MySQL 反向索引。DSAR
会持久化来源墓碑和待清除 key，Redis 故障时先阻断读取与重建，恢复后后台
重试物理清除。

## 4. 端口与服务发现

容器间通信必须使用服务名和容器端口；宿主端口只用于本机诊断。应用端口由
`COMPOSE_APP_BIND_HOST` 控制，数据库、Adminer 和模型 API 由
`COMPOSE_PRIVATE_BIND_HOST` 控制；两者默认都是 `127.0.0.1`，不会直接暴露
到局域网或公网。

| 服务 | 容器内地址 | 默认宿主地址 |
|---|---|---|
| MySQL | `mysql:3306` | `127.0.0.1:3307` |
| Redis（可选，无宿主端口） | `redis:6379` | 不发布 |
| Adminer | `adminer:8080` | `127.0.0.1:8081` |
| backend | `backend:8000` | `127.0.0.1:8000` |
| frontend | `frontend:5173` | `127.0.0.1:5173` |
| vLLM strong | `vllm-strong:8000` | `127.0.0.1:18000` |
| vLLM weak | `vllm-weak:8000` | `127.0.0.1:18001` |
| BGE-M3 | `bge-m3:80` | `127.0.0.1:18080` |
| funASR | `funasr:8000` | `127.0.0.1:10095` |
| CLAP | `clap-service:8006` | `127.0.0.1:18006` |
| CAM++ | `campplus-service:8007` | `127.0.0.1:18007` |

这消除了后端与 `vllm-strong` 同时发布宿主机 `8000` 的冲突。宿主端口可通过
`*_HOST_PORT` 环境变量修改。

如确需让反向代理从其他主机访问前后端，可只开放应用端口：

```dotenv
COMPOSE_APP_BIND_HOST=0.0.0.0
COMPOSE_PRIVATE_BIND_HOST=127.0.0.1
```

生产环境应优先保留回环绑定，由同机反向代理只转发 frontend/backend。不要
把 `COMPOSE_PRIVATE_BIND_HOST` 改为 `0.0.0.0`；如果隔离网络中的远程推理
节点确实需要访问，应同时配置宿主防火墙、TLS、认证，并将
`MYSQL_ROOT_PASSWORD`、`MYSQL_PASSWORD`、`JWT_SECRET`、`OPENAI_API_KEY`
替换为独立强密钥。Adminer 不应在生产 profile 启动。

## 5. VAD 部署边界

### 5.1 Batch VAD

旧 Compose 使用的 `jetresearch/silero-vad-server:latest` 无法验证公开版本和
镜像来源，已从拓扑移除。不要用虚构 tag 代替可审计供应链。

Batch VAD 默认保持：

```dotenv
ADAPTER_VAD_MODE=mock
SILERO_VAD_URL=http://silero-vad.invalid:8000
```

如需真实 batch VAD，用户必须提供经过审计、兼容以下契约的外部服务，并将
`SILERO_VAD_URL` 指向它：

- `POST /v1/vad/segment`
- multipart 字段 `audio`、`min_segment_sec`、`max_segment_sec`
- 返回 `segments[].start_sec/end_sec/confidence`

### 5.2 Streaming VAD

Streaming VAD 使用项目内 ONNX adapter，不依赖 batch VAD HTTP 容器。使用
只读模型挂载 overlay：

```bash
SILERO_VAD_MODEL_FILE=/absolute/path/silero_vad.onnx \
docker compose \
  -f docker-compose.yml \
  -f docker-compose.streaming-vad.yml \
  --profile mock up -d
```

Overlay 会设置：

```dotenv
ADAPTER_STREAMING_VAD_MODE=real
SILERO_VAD_MODEL_PATH=/models/silero_vad.onnx
```

## 6. 静态验证（不拉模型）

```bash
for profile in mock cache-redis models-cpu models-single-gpu models-multi-gpu; do
  docker compose --env-file /dev/null \
    --profile "$profile" config --quiet
done

cd backend
uv run pytest tests/infrastructure/test_compose_profiles.py -q --no-cov
```

配置测试覆盖：

- 每个 profile 的精确服务集合；
- 宿主端口不冲突；
- 所有宿主端口默认仅绑定 `127.0.0.1`；
- 所有镜像禁止 `:latest`；
- GPU 服务只保留一个显式 `device_id`；
- 模型服务健康检查与基础安全边界；
- CLAP/CAM++ 非 root 与只读根文件系统；
- streaming VAD ONNX 挂载只读；
- 后端使用正确的容器内端口。

## 7. 常见问题

### vLLM 启动时 OOM

降低 `VLLM_*_GPU_MEMORY_UTILIZATION`，换用更小或量化模型，或把
`BGE_M3_GPU_ID` / `CLAP_GPU_ID` 调整到其他 GPU。不要让多个大模型在单卡上
各自声明接近 `0.9` 的显存利用率。

### 单卡模式 weak 请求失败

确认 `.env` 同时满足：

```dotenv
OPENAI_BASE_URL_WEAK=http://vllm-strong:8000/v1
LLM_WEAK_MODEL=qwen3.6-27b
```

### BGE 连接失败

容器内 URL 必须是 `http://bge-m3:80`，不能写宿主调试端口 `18080`。
CPU/GPU 两个服务通过网络别名统一为 `bge-m3`。

### 模型缓存每次重下

检查 `audiography_vllm_cache`、`audiography_tei_cache`、
`audiography_funasr_cache`、`audiography_clap_cache` 和
`audiography_campplus_cache` 是否存在。不要执行
`docker compose down -v`，该命令会删除模型与数据库卷。

### CLAP/CAM++ 权限错误

两个镜像以 UID/GID `10001` 运行。Compose 命名卷会自动提供容器内缓存；
如果改成宿主 bind mount，必须预先把目录授权给 `10001:10001`。
