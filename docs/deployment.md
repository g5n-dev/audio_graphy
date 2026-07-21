# AudioGraphy Deployment Guide

> 部署指南 — covers mock mode (zero-dependency dev) and real mode (GPU-backed
> vLLM + Silero + bge-m3). M4 code-ready; M5 will add funASR.

| Section | What you get |
|---|---|
| [§1 Hardware](#1-hardware-requirements) | min vRAM / disk / GPU per topology |
| [§2 Quick start](#2-quick-start) | one command for mock, one for real |
| [§3 Model download](#3-model-download) | HF CLI + `HF_TOKEN` for gated Qwen |
| [§4 Per-adapter enablement](#4-per-adapter-enablement) | flip one env var per adapter |
| [§5 Mixed mode](#5-mixed-mode) | e.g. real VAD + mock LLM |
| [§6 Troubleshooting](#6-troubleshooting-faq) | 10 most common failures + fixes |
| [§7 Environment reference](#7-environment-variable-reference) | full env table |

---

## 1. Hardware requirements

| Topology | CPU | RAM | GPU | Disk | Use case |
|---|---|---|---|---|---|
| **Dev / CI** | 4 cores | 4 GB | none | 5 GB | mock adapters only — runs anywhere |
| **VAD-only** | 4 cores | 8 GB | optional (Silero runs on CPU) | 10 GB | speech-segmentation evaluation |
| **Embed-only** | 4 cores | 8 GB | 1× 16 GB (e.g. T4 / 4090) | 15 GB | bge-m3 indexing |
| **Full real** | 8 cores | 32 GB | **2× A100 80 GB** or **2× 4090** (24 GB each) | 200 GB | full pipeline |

### Per-service vRAM budget (full real)

| Service | Image | vRAM peak | Notes |
|---|---|---|---|
| `vllm-strong` (Qwen3.6-27B FP16) | `vllm/vllm-openai:v0.7.2` | ~55 GB | 27B weights + KV cache (`--gpu-memory-utilization 0.90`) |
| `vllm-weak` (Qwen3.6-35B-A3B FP16) | `vllm/vllm-openai:v0.7.2` | ~70 GB | MoE — only 3B activated parameters per token |
| `bge-m3` | `ghcr.io/huggingface/text-embeddings-inference:1.5` | ~3 GB | small but required |
| `silero-vad` | `jetresearch/silero-vad-server:latest` | <1 GB | CPU-only OK |
| `funasr` (M5 placeholder) | `funasr-runtime-sdk-online-cpu-0.1.12` | — | CPU image, M4 does NOT call it |

> **Single-GPU alternative**: run `vllm-strong` and `vllm-weak` sequentially
> with `--tensor-parallel-size 1` and `--gpu-memory-utilization 0.45` each,
> OR put them on different GPUs with `deploy.resources.reservations.devices`.

### Validate GPU before first run

```bash
nvidia-smi                                                 # host driver check
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
docker compose --profile real up -d vllm-strong
docker compose --profile real logs --tail=50 vllm-strong  # watch for OOM
```

---

## 2. Quick start

### 2.1 Mock mode (zero dependencies, dev/CI default)

```bash
cp .env.example .env                  # all defaults are mock-safe
docker compose up -d                  # mysql + adminer + backend + frontend
curl http://localhost:8000/health     # → {"status":"ok"}
```

No GPU, no model download, no `HF_TOKEN`. Backend uses `MockVADAdapter`,
`MockASRAdapter`, `MockLLMAdapter` × 2, `MockEmbedAdapter`.

### 2.2 Real mode (all 4 real adapters)

```bash
cp .env.example .env
# Edit .env:
#   ADAPTER_VAD_MODE=real
#   ADAPTER_LLM_MODE=real
#   ADAPTER_EMBED_MODE=real
#   JWT_SECRET=<32+ random chars>            # required when any mode is real
#   HF_TOKEN=hf_<your token>                 # required for gated Qwen models

docker compose --profile real up -d    # 9 services total
docker compose --profile real ps       # wait until all "healthy"
curl http://localhost:8000/health
```

> **`ADAPTER_ASR_MODE=real` is rejected by the backend validator** — funASR
> lands in M5. Leave it `mock`.

---

## 3. Model download

### 3.1 Required HuggingFace weights

| Service | Repo ID | Gated? | Size |
|---|---|---|---|
| vllm-strong | `Qwen/Qwen3.6-27B` | **yes** | ~55 GB |
| vllm-weak | `Qwen/Qwen3.6-35B-A3B` | **yes** | ~70 GB |
| bge-m3 | `BAAI/bge-m3` | no | ~2 GB |
| silero-vad | baked into image | — | — |

### 3.2 Auth for gated models

```bash
pip install -U "huggingface_hub[cli]"
huggingface-cli login                  # paste your HF token
# or set HF_TOKEN in .env — compose passes it through to vLLM/TEI containers
```

The `vllm_cache` volume caches weights across container restarts, so the
~125 GB download only happens once.

### 3.3 Pre-pull (optional, recommended for offline hosts)

```bash
docker compose --profile real pull vllm-strong vllm-weak bge-m3 silero-vad funasr
```

---

## 4. Per-adapter enablement

Each adapter is an independent switch. The backend `Settings` validator reads
them at startup — see `backend/audio_graphy/config.py`.

| To enable | Env var | Compose service |
|---|---|---|
| Silero VAD | `ADAPTER_VAD_MODE=real` | `silero-vad` |
| vLLM strong+weak LLM | `ADAPTER_LLM_MODE=real` | `vllm-strong`, `vllm-weak` |
| bge-m3 embedding | `ADAPTER_EMBED_MODE=real` | `bge-m3` |
| funASR (M5) | `ADAPTER_ASR_MODE=real` | **rejected in M4** |

> **Important**: `ADAPTER_MODE` (legacy global) does **not** auto-propagate to
> the per-adapter fields. You must set each of the 4 fields explicitly.
> This was a deliberate simplification (Q5, see `docs/m4-architecture.md` §1.6).

---

## 5. Mixed mode

You can mix mock + real freely. Only the services whose mode is `real` need
to be started.

### 5.1 Example: real VAD + real embed, mock LLM (low-cost indexing box)

```dotenv
ADAPTER_VAD_MODE=real
ADAPTER_LLM_MODE=mock
ADAPTER_EMBED_MODE=real
```

```bash
docker compose --profile real up -d silero-vad bge-m3 backend mysql
```

### 5.2 Example: only real LLM (debugging prompt quality)

```dotenv
ADAPTER_LLM_MODE=real
```

```bash
docker compose --profile real up -d vllm-strong vllm-weak backend mysql
```

### 5.3 What happens internally

The `build_adapters()` factory inspects the 4 modes; if **any** is `"real"` it
calls `build_hybrid_bundle()` which constructs each adapter independently:

| Adapter | mock | real |
|---|---|---|
| VAD | `MockVADAdapter` | `SileroVADAdapter` (url from `SILERO_VAD_URL`) |
| ASR | `MockASRAdapter` | **always mock in M4** |
| LLM strong+weak | `MockLLMAdapter` × 2 | `LLMOpenAIAdapter` × 2 (urls from `OPENAI_BASE_URL_*`) |
| Embed | `MockEmbedAdapter` | `BGEEmbedAdapter` (url from `BGE_M3_URL`) |

Startup log shows the chosen combination:
`Building HYBRID adapter bundle (asr=mock vad=real llm=mock embed=real)`.

---

## 6. Troubleshooting FAQ

### 6.1 vLLM container OOM on startup
**Symptom**: `CUDA out of memory` in `docker compose logs vllm-strong`.
**Fix**: lower `--gpu-memory-utilization 0.90` to `0.70` in compose, or upgrade
to A100 80 GB. Two 4090s (24 GB each) are NOT enough for Qwen3.6-27B FP16 —
switch to AWQ quantized weights in M5+.

### 6.2 vLLM returns HTTP 500 on `/v1/chat/completions`
**Symptom**: backend logs `LLMServerError: LLM 500`.
**Diagnostic**: `docker compose --profile real logs --tail=100 vllm-strong`.
**Common causes**: model name mismatch (the `--served-model-name` in compose
MUST equal `LLM_STRONG_MODEL` in `.env`), prompt exceeds context window, or
weights partially downloaded (delete `vllm_cache` volume and re-pull).

### 6.3 bge-m3 dim mismatch
**Symptom**: backend logs `EmbedDimMismatchError: expected 1024, got 512`.
**Cause**: TEI container launched with the wrong `--model-id` (e.g. bge-small
instead of bge-m3). Verify compose `command:` is `--model-id BAAI/bge-m3`.
Also check `EMBEDDING_DIM=1024` in `.env`.

### 6.4 Silero VAD image trust warning
**Symptom**: "should I trust `jetresearch/silero-vad-server:latest`?"
**Context**: this image is **community-maintained, NOT official Silero**.
**Mitigation**:
1. Pin a digest: `image: jetresearch/silero-vad-server@sha256:<digest>`.
2. Audit the source: <https://github.com/jetresearch/silero-vad-server>.
3. Or fall back to `ADAPTER_VAD_MODE=mock` until you self-host.

### 6.5 `REAL adapter ON but JWT_SECRET is placeholder` warning
**Symptom**: backend startup logs this warning.
**Fix**: edit `.env` and set `JWT_SECRET=<32+ random chars>`. The validator
emits this whenever any `ADAPTER_*_MODE=real` is enabled. It does NOT block
startup, but real services should never run with the placeholder secret.

### 6.6 Network timeout calling real adapter
**Symptom**: `LLMTimeoutError` / `VADTimeoutError` in backend logs.
**Fix**: check the target container is healthy (`docker compose ps`), check
DNS resolution from inside the backend container
(`docker compose exec backend python -c "import socket; print(socket.gethostbyname('vllm-strong'))"`),
verify the URL in `.env` has the right port (`8000` for vLLM internal,
`8002` for Silero external, `8080` for bge-m3).

### 6.7 Port conflict on host (8000 / 8001 / 8002 / 8080)
**Symptom**: `bind: address already in use` when running `docker compose up`.
**Fix**: change the LEFT side of the `ports:` mapping
(e.g. `"18000:8000"`). The backend talks to containers over the internal
docker network, so the host port mapping is only for your direct testing.

### 6.8 `--served-model-name` mismatch
**Symptom**: `LLMBadRequest: model not found` from vLLM.
**Fix**: the model name the backend sends (from `LLM_STRONG_MODEL`) MUST
exactly match `--served-model-name` in the vLLM compose command.
`.env` default `LLM_STRONG_MODEL=qwen3.6-27b` matches compose
`--served-model-name qwen3.6-27b`. Case-sensitive.

### 6.9 File permission errors in `working_dir`
**Symptom**: backend logs `PermissionError: /data/working_dir/...`.
**Fix**: `docker compose down && sudo chown -R 1000:1000 ./working_dir`
(or whatever UID the backend container runs as). The `working_dir` volume
must be writable by the backend for VideoRAG file index.

### 6.10 Volume mount issues — model re-downloads every restart
**Symptom**: each `docker compose --profile real up` re-pulls ~125 GB.
**Fix**: confirm `vllm_cache` and `tei_cache` volumes exist:
`docker volume ls | grep audiography`. If missing, the compose YAML in
`docker-compose.yml` was edited — the `volumes:` block at the bottom must
declare both. Don't `docker compose down -v` (the `-v` flag wipes volumes).

---

## 7. Environment variable reference

| Var | Default | Purpose |
|---|---|---|
| `ADAPTER_MODE` | `mock` | legacy global — does NOT drive resolution (M4) |
| `ADAPTER_ASR_MODE` | `mock` | M4 — must be `mock` (funASR lands in M5) |
| `ADAPTER_VAD_MODE` | `mock` | `real` → `silero-vad` service |
| `ADAPTER_LLM_MODE` | `mock` | `real` → `vllm-strong` + `vllm-weak` |
| `ADAPTER_EMBED_MODE` | `mock` | `real` → `bge-m3` service |
| `SILERO_VAD_URL` | `http://silero-vad:8002` | adapter appends `/v1/vad/segment` |
| `BGE_M3_URL` | `http://bge-m3:8080` | adapter appends `/v1/embeddings` |
| `OPENAI_BASE_URL_STRONG` | `http://vllm-strong:8000/v1` | full path with `/v1` |
| `OPENAI_BASE_URL_WEAK` | `http://vllm-weak:8001/v1` | full path with `/v1` |
| `OPENAI_API_KEY` | `dummy` | vLLM ignores value; required by OpenAI schema |
| `LLM_STRONG_MODEL` | `qwen3.6-27b` | MUST match vLLM `--served-model-name` |
| `LLM_WEAK_MODEL` | `qwen3.6-35b-a3b` | MUST match vLLM `--served-model-name` |
| `EMBEDDING_DIM` | `1024` | bge-m3 native dim — do not change |
| `HF_TOKEN` | empty | required for gated Qwen models on HuggingFace |
| `JWT_SECRET` | `change-me-...` | **override when any `ADAPTER_*_MODE=real`** |
| `MYSQL_HOST` / `MYSQL_PORT` | `mysql` / `3306` | docker-internal |
| `WORKING_DIR` | `/data/working_dir` | VideoRAG file index root |

---

**Owner**: 寇豆码 (backend) · **Reviewer**: 高见远 (architect) · **Sign-off**: 齐活林 (PM)
