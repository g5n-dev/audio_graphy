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
`master-key-init`，在独立 `${COMPOSE_RESOURCE_PREFIX:-audiography}_master_key` 卷中生成或校验 0600
Fernet 密钥；backend 仅以只读方式挂载该卷，并在启动时立即校验。密钥缺失或
损坏时服务失败关闭，不会退回明文录音。该卷必须与音频数据分开备份，删除它会
使既有密文不可恢复。Kubernetes/生产编排应改为外部 KMS 或 Secret 挂载同一路径，
并移除本地初始化器。

### 在同一台主机上运行第二套栈

`docker compose -p <名字>` **不足以隔离，从来都不够**：本 compose 文件显式命名了
网络、全部数据卷和容器。容器名冲突会报错，但**网络名和卷名的冲突是静默的**——
docker 只打一行警告，两套栈照常启动，然后共用同一个网络（`mysql` 这个主机名会
解析到两台数据库并轮询）和同一组卷（包括主密钥卷）。曾经就是这样，一套验收栈把
数据写进了另一套的生产数据库。

正确做法，三件事缺一不可，全部写在**第二套栈自己的** `.env` 里：

1. `COMPOSE_RESOURCE_PREFIX=<名字>` —— 隔离项目名、网络、卷、容器；
2. 所有 `*_HOST_PORT` 换成空闲端口 —— 端口是故意不参数化的，冲突会响亮地失败，
   这是最后一道保险；
3. `DEPLOYMENT_ID=<同一名字>` —— MySQL 的 `GET_LOCK` 是服务器级而非库级，
   两套栈即使分库也会争抢对方的锁，超时后双双失去序列化保护。

设置前缀等于选择**一整套独立数据**：带前缀的栈从空数据库和新主密钥启动，卷不会
迁移。绝不要在主栈上设置它——数据会"看起来消失"（其实还在 `audiography_*` 卷里，
只是没有任何东西挂载它们），而此时最本能的清理命令 `docker compose down -v`
（前缀未设时）会真正删掉它们。

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

ADAPTER_VAD_MODE=real

ADAPTER_LLM_MODE=mock
ADAPTER_AUDIO_EMBED_MODE=mock
```

`ADAPTER_VAD_MODE=real` 需要那 2 MB 的 ONNX 就位，否则 VAD 容器会一直
unhealthy——先取一次，三个 models profile 共用同一个文件（见 5.1）：

```bash
mkdir -p models && curl -Lo models/silero_vad.onnx \
  https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
```

然后启动：

```bash
docker compose --profile models-cpu up -d
docker compose --profile models-cpu ps
```

CPU profile 不含 CLAP，因为 CLAP 服务在本项目中强制使用 CUDA。

**注意：`models-cpu` 不含任何 LLM**——上面把 `ADAPTER_LLM_MODE` 留在 mock 正是
因此。抽取和问答要产生真实结论，必须再给 LLM 一个后端：

```bash
# 追加 CPU LLM（compose 内置 Ollama，CPU 推理，速度有限）：
docker compose --profile models-cpu --profile models-cpu-llm up -d
docker exec $(docker compose ps -q ollama) ollama pull qwen2.5:7b
```

并在 `.env` 中：

```dotenv
ADAPTER_LLM_MODE=real
OPENAI_BASE_URL_STRONG=http://ollama:11434/v1
OPENAI_BASE_URL_WEAK=http://ollama:11434/v1
LLM_STRONG_MODEL=qwen2.5:7b
LLM_WEAK_MODEL=qwen2.5:7b
OPENAI_API_KEY=ollama
```

macOS Docker Desktop 下容器内 Ollama 没有 Metal 加速，宿主机直装 Ollama 更快：
保持 profile 不变，把两个 base URL 换成
`http://host.docker.internal:11434/v1` 即可（详见 `.env.example` 的
No-GPU alternative 注释块）。

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
ADAPTER_VAD_MODE=real

OPENAI_BASE_URL_STRONG=http://vllm-strong:8000/v1
OPENAI_BASE_URL_WEAK=http://vllm-strong:8000/v1
LLM_STRONG_MODEL=qwen3.6-27b
LLM_WEAK_MODEL=qwen3.6-27b
```

同样需要先取 VAD 的 ONNX（见 3.2 / 5.1）：

```bash
mkdir -p models && curl -Lo models/silero_vad.onnx \
  https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx

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
ADAPTER_VAD_MODE=real

OPENAI_BASE_URL_STRONG=http://vllm-strong:8000/v1
OPENAI_BASE_URL_WEAK=http://vllm-weak:8000/v1
```

同样需要先取 VAD 的 ONNX（见 3.2 / 5.1）：

```bash
mkdir -p models && curl -Lo models/silero_vad.onnx \
  https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx

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

`silero-vad-service` 随三个 models profile 一起启动，镜像由本仓库的
`docker/silero-vad-service/Dockerfile` 构建。这里曾经指向
`jetresearch/silero-vad-server:latest`——一个无法验证来源的 tag，当时的处置是
把它从拓扑里删掉并让用户自备外部服务。现在不需要了：不用虚构 tag 的正确解法
是自己构建，而不是把这一段流水线永久留在 mock 上。

模型权重仍由操作者提供，和流式那条用的是同一个文件。~2 MB、MIT 许可，本仓库
不代为分发、也不为一个自己不构建的模型二进制背书：

```bash
mkdir -p models && curl -Lo models/silero_vad.onnx \
  https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
```

放好后开启真实 VAD：

```dotenv
ADAPTER_VAD_MODE=real
SILERO_VAD_MODEL_FILE=./models/silero_vad.onnx
```

文件缺失时容器会一直 unhealthy 并在 `/health` 里说明原因，不会静默返回空
segments——那种退化和「这段录音没有人说话」在下游无法区分。

**保持 mock 的后果**：切分点由文件大小推算，与语音内容无关。后面的 ASR、
说话人聚类、标签抽取全部建立在这组切分之上，所以这不是「精度略低」，是整条
流水线的输入是假的。

沿用外部 VAD 服务同样支持——把 `SILERO_VAD_URL` 指向它即可，契约是：

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

## 5.3 首次开启声纹后的存量回填

`ENABLE_VOICEPRINT=true` 只对**此后入库**的录音生效。此前的录音是在
diarization 关闭的状态下切分的，段上没有说话人标签，不会自己长出跨录音身份。
开启后需要跑一次回填，否则说话人库只覆盖新数据：

```bash
# 先看有多少待回填（不产生任何写入与推理开销）
docker compose exec backend python scripts/backfill_voiceprints.py \
  --tenant chang_an --dry-run

# 分批处理；每条录音需要一次整文件 diarization，务必在低峰期执行
docker compose exec backend python scripts/backfill_voiceprints.py \
  --tenant chang_an --limit 50
```

脚本会按录音 ID 游标自动连跑多批直到没有待处理项（`--max-batches` 兜底，默认 20 批）。
用游标而不是"还没有链接"来推进是必要的：音频已被保留期清理、无语音、无人过质量门的
录音永远不会产生链接，只按"未链接"筛选会让这些录音把每一批都填满，后面的永远轮不到。

重复运行是安全的：已链接的说话人会被逐个跳过，中途失败的录音下次会**只补没做完的
那部分**。脚本与在线管线共用同一把租户锁，可以在系统运行期间执行。详见
[ADR-0001](./adr/0001-voiceprint-sampling.md)。

## 5.4 校准声纹合并阈值

`VOICEPRINT_COSINE_THRESHOLD` / `VOICEPRINT_AMBIGUOUS_THRESHOLD` 是普通配置项，
改起来很容易；难的是知道该改成多少。默认值 0.5 / 0.7 从未针对本部署的音频校准过。
先准备试验对文件（每行 `<注册音频> <测试音频> <0|1>`）。语料选型 `docs/DESIGN.md` §8
已有裁决：**声纹 EER 用 CN-Celeb**，AliMeeting 是 DER（说话人分离）的基准，不是这里用的。
`scripts/build_voiceprint_trials.py` 负责生成：

```bash
# 布局一：一个说话人一个目录（CN-Celeb 的 data/ 树，也适合自建标注集）
python scripts/build_voiceprint_trials.py \
  --from-dir /data/CN-Celeb/data --out /data/eval/trials.txt

# 布局二：CN-Celeb 官方评测列表（结果可与公开数字对比，CAM++ 约 6.8% EER）
python scripts/build_voiceprint_trials.py \
  --from-cnceleb-trials /data/CN-Celeb/eval/lists/trials.lst \
  --enroll-list /data/CN-Celeb/eval/enroll/lst \
  --audio-root /data/CN-Celeb/eval \
  --out /data/eval/trials.txt
```

采样是定过种子的，两次运行结果可比；引用不存在音频的配对会被丢弃并计数，
而不是留到打分阶段静默缩水。异人配对少于 100 对时会告警——1% 的误接受率
目标从那么少的样本里估不出来。

然后跑：

```bash
docker compose exec backend python scripts/calibrate_voiceprint_thresholds.py \
  --trials /data/eval/trials.txt
```

脚本会用**当前配置的** adapter 提取声纹、统计同人/异人余弦分布，并给出两个阈值的
建议值；它只打印建议，不写任何配置。输出会标明用的是哪种 adapter 模式——
`mock` 模式可以完整验证这条工作流，但不构成对真实音频的建议。

mock 模式下还需加 `--mock-speaker-from dirname|filename` 才能得到有意义的分布：
mock 听不见声音，默认把每个文件当成不同的人（EER 恒为 0.5，工具会如实报告
"无等错误率点"）。`dirname` 适用于一个说话人一个目录的语料（CN-Celeb 的
`data/` 树），`filename` 适用于扁平目录里按说话人命名的文件。两者不能互相猜：
前一种布局下文件名前缀是所有人共有的录制类型，后一种布局下父目录是所有人
共有的，选错会把整个语料塌成一个人。对时间戳或 UUID 命名的文件两种都没有意义。

两个阈值的含义不同：`AMBIGUOUS_THRESHOLD` 取在目标误接受率（`--max-far`，默认 1%）
处，是"可以静默合并"的下限；`COSINE_THRESHOLD` 取在等错误率点，是"完全不合并"的
下限。两者之间的分数会合并但标记 AMBIGUOUS 并在检索中降权。

如果两个建议值相同，说明这批试验对完全可分、不存在模糊地带——在真实录音上出现
这种结果通常意味着试验集不够有代表性（说话人太少，或片段太干净太长）。

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

用 `docker volume ls` 检查各模型缓存卷是否存在——`vllm_cache`、`tei_cache`、
`funasr_cache`、`clap_cache`、`campplus_cache`，卷名以
`${COMPOSE_RESOURCE_PREFIX:-audiography}_` 为前缀（默认 `audiography_`），
只有对应 profile 启动过的卷才会存在。不要执行
`docker compose down -v`，该命令会删除模型与数据库卷。

### CLAP/CAM++ 权限错误

两个镜像以 UID/GID `10001` 运行。Compose 命名卷会自动提供容器内缓存；
如果改成宿主 bind mount，必须预先把目录授权给 `10001:10001`。
