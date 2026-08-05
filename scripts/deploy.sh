#!/usr/bin/env bash
# ============================================================
# AudioGraphy 单机部署脚本
#
# 覆盖从零到可登录的完整路径:
#   ./scripts/deploy.sh init --profile cpu   # 生成 .env(随机密钥)+ 写入 profile 配置
#   ./scripts/deploy.sh model                # 下载 Silero VAD 权重(2 MB,models profile 必需)
#   ./scripts/deploy.sh up                   # 启动并等待健康
#   ./scripts/deploy.sh admin --email you@example.com   # 创建首个管理员
#   ./scripts/deploy.sh verify               # 部署体检
#
# profile 对应关系(与 docker-compose.yml / docs/deployment.md 一致):
#   mock       仅核心应用,模型全 mock,零下载零 GPU
#   cpu        models-cpu + models-cpu-llm(funASR/BGE-M3/CAM++/VAD + Ollama)
#   gpu        models-single-gpu(CPU 模型 + 单卡 vLLM)
#   gpu-multi  models-multi-gpu(双 vLLM 分卡)
#
# 离线/内网(私有化)部署配合 scripts/offline_bundle.sh:
# 在有网机器上导出镜像与模型缓存,目标机 import 后 `up <profile> --offline`。
# ============================================================
set -euo pipefail

ROOT="${AG_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"
MODEL_FILE="$ROOT/models/silero_vad.onnx"
MODEL_URL="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
PROD_FRONTEND_OVERLAY="$ROOT/docker-compose.frontend-prod.yml"
BLOCK_BEGIN="# >>> deploy.sh managed profile block >>>"
BLOCK_END="# <<< deploy.sh managed profile block <<<"

info()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------
# profile → compose --profile 参数
# ------------------------------------------------------------
compose_profiles() {
  case "$1" in
    mock)      echo "--profile mock" ;;
    cpu)       echo "--profile models-cpu --profile models-cpu-llm" ;;
    gpu)       echo "--profile models-single-gpu" ;;
    gpu-multi) echo "--profile models-multi-gpu" ;;
    *) die "未知 profile: $1(可选 mock | cpu | gpu | gpu-multi)" ;;
  esac
}

# 记录在 .env 管理块首行,`up` 不带参数时沿用
saved_profile() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^# profile=//p" "$ENV_FILE" | head -1
}

require_env() {
  [ -f "$ENV_FILE" ] || die ".env 不存在——先运行: ./scripts/deploy.sh init --profile <mock|cpu|gpu|gpu-multi>"
}

# 48 位字母数字随机串;不依赖 openssl(目标机可能没有)。
# 先读定量再过滤:tr 读无限流会在 head 关管道时吃 SIGPIPE,pipefail 下整条命令 141。
gen_secret() {
  local s=""
  while [ "${#s}" -lt 48 ]; do
    s="$s$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"
  done
  printf '%s' "${s:0:48}"
}

# ------------------------------------------------------------
# init:生成 .env + 随机密钥 + profile 管理块
# ------------------------------------------------------------
profile_block() {
  # 各 profile 的 adapter 配置,与 docs/deployment.md §3 的 dotenv 块逐字一致。
  # 追加在文件末尾:dotenv 语义按最后一次赋值生效,所以这里覆盖上文同名变量。
  case "$1" in
    mock) cat <<'EOF'
# mock:全部 adapter 保持 mock,无需任何模型
EOF
      ;;
    cpu) cat <<'EOF'
ADAPTER_ASR_MODE=real
ADAPTER_EMBED_MODE=real
ADAPTER_VOICEPRINT_MODE=real
ENABLE_VOICEPRINT=true
ADAPTER_VAD_MODE=real
ADAPTER_LLM_MODE=real
OPENAI_BASE_URL_STRONG=http://ollama:11434/v1
OPENAI_BASE_URL_WEAK=http://ollama:11434/v1
LLM_STRONG_MODEL=qwen2.5:7b
LLM_WEAK_MODEL=qwen2.5:7b
OPENAI_API_KEY=ollama
EOF
      ;;
    gpu) cat <<'EOF'
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
EOF
      ;;
    gpu-multi) cat <<'EOF'
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
EOF
      ;;
  esac
}

write_managed_block() {
  local profile="$1" tmp
  tmp="$(mktemp)"
  # 删旧块(awk 而非 sed -i:macOS 与 GNU 的 -i 语义不同)
  awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    !skip { print }
  ' "$ENV_FILE" >"$tmp"
  {
    cat "$tmp"
    echo "$BLOCK_BEGIN"
    echo "# profile=$profile"
    echo "# 由 deploy.sh init 写入,按 dotenv 后者覆盖前者的语义覆盖上文同名变量。"
    echo "# 手工改动请写在块外或换 profile 重新 init;块内内容会被下次 init 重写。"
    profile_block "$profile"
    echo "$BLOCK_END"
  } >"$ENV_FILE"
  rm -f "$tmp"
}

cmd_init() {
  local profile=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile="$2"; shift 2 ;;
      *) die "init 不认识的参数: $1" ;;
    esac
  done
  [ -n "$profile" ] || die "init 需要 --profile <mock|cpu|gpu|gpu-multi>"
  compose_profiles "$profile" >/dev/null   # 校验取值

  if [ ! -f "$ENV_FILE" ]; then
    [ -f "$ENV_EXAMPLE" ] || die "找不到 $ENV_EXAMPLE"
    local mysql_pw root_pw jwt tmp
    mysql_pw="$(gen_secret)"; root_pw="$(gen_secret)"; jwt="$(gen_secret)"
    tmp="$(mktemp)"
    # 只重写三个必须随机化的密钥;其余逐行保留,注释与分区原样可读
    awk -v mp="$mysql_pw" -v rp="$root_pw" -v jw="$jwt" '
      /^MYSQL_PASSWORD=/      { print "MYSQL_PASSWORD=" mp; next }
      /^MYSQL_ROOT_PASSWORD=/ { print "MYSQL_ROOT_PASSWORD=" rp; next }
      /^JWT_SECRET=/          { print "JWT_SECRET=" jw; next }
      { print }
    ' "$ENV_EXAMPLE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    info "已生成 .env(MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD / JWT_SECRET 均为随机值,权限 600)"
  else
    info ".env 已存在——密钥保持不动,只更新 profile 管理块"
  fi

  write_managed_block "$profile"
  info "profile 设为 $profile(写入 .env 末尾的管理块)"
  if [ "$profile" != "mock" ]; then
    info "下一步: ./scripts/deploy.sh model && ./scripts/deploy.sh up"
  else
    info "下一步: ./scripts/deploy.sh up"
  fi
}

# ------------------------------------------------------------
# model:下载 VAD 权重
# ------------------------------------------------------------
cmd_model() {
  if [ -f "$MODEL_FILE" ]; then
    info "VAD 模型已就位: $MODEL_FILE"
    return 0
  fi
  mkdir -p "$ROOT/models"
  info "下载 Silero VAD ONNX(约 2 MB,MIT 许可,仓库不代为分发)…"
  curl -fSL --retry 3 -o "$MODEL_FILE" "$MODEL_URL" \
    || die "下载失败;离线环境请从有网机器复制同一文件到 $MODEL_FILE"
  info "完成: $MODEL_FILE"
}

# ------------------------------------------------------------
# up:启动并等待健康
# ------------------------------------------------------------
cmd_up() {
  require_env
  local profile="" offline=0 dev_frontend=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --offline) offline=1; shift ;;
      --dev-frontend) dev_frontend=1; shift ;;
      -*) die "up 不认识的参数: $1" ;;
      *) profile="$1"; shift ;;
    esac
  done
  profile="${profile:-$(saved_profile)}"
  [ -n "$profile" ] || die "未指定 profile 且 .env 中没有管理块——运行 init --profile 或 up <profile>"
  local flags; flags="$(compose_profiles "$profile")"

  if [ "$profile" != "mock" ] && [ ! -f "$MODEL_FILE" ]; then
    die "缺少 ${MODEL_FILE}——models profile 的 VAD 容器没有它无法健康。先运行: ./scripts/deploy.sh model"
  fi

  local extra=""
  if [ "$offline" -eq 1 ]; then
    # 镜像已由 offline_bundle.sh import;禁止 build 与 pull,缺镜像直接失败而非出网
    extra="--no-build --pull never"
  fi

  # 模型服务首启要下载权重(可达 10 GB),等待窗口放宽
  local timeout=300
  [ "$profile" != "mock" ] && timeout=1800

  # 部署默认生产前端(构建产物 + nginx):样式与资产全在镜像内,离线
  # 一致;Vite dev server 只在显式 --dev-frontend 时保留(开发热更)。
  local files="-f $ROOT/docker-compose.yml"
  [ "$dev_frontend" -eq 0 ] && files="$files -f $PROD_FRONTEND_OVERLAY"

  info "启动 profile=$profile(healthcheck 等待上限 ${timeout}s)…"
  # shellcheck disable=SC2086
  docker compose $files $flags up -d --wait --wait-timeout "$timeout" $extra || {
    warn "有容器未在时限内变健康;当前状态:"
    # shellcheck disable=SC2086
    docker compose $files $flags ps
    die "排查: ./scripts/deploy.sh logs <service>;VAD 容器 unhealthy 通常是模型文件缺失"
  }

  if [ "$profile" = "cpu" ]; then
    local llm_model
    llm_model="$(sed -n 's/^LLM_STRONG_MODEL=//p' "$ENV_FILE" | tail -1)"
    llm_model="${llm_model:-qwen2.5:7b}"
    if [ "$offline" -eq 1 ]; then
      info "离线模式:跳过 ollama pull,依赖导入的 ollama_models 卷(应含 $llm_model)"
    else
      info "拉取 LLM 权重 $llm_model(首次约 5 GB,已缓存则秒回)…"
      # shellcheck disable=SC2086
      docker compose $files $flags exec -T ollama ollama pull "$llm_model" \
        || warn "ollama pull 失败——问答/抽取会退回固定文案;稍后可手动重试同一命令"
    fi
  fi

  info "启动完成。建议接着跑: ./scripts/deploy.sh verify"
}

# ------------------------------------------------------------
# admin:创建首个管理员(包装 compose 的 bootstrap-admin one-shot)
# ------------------------------------------------------------
cmd_admin() {
  require_env
  local email="" password="${BOOTSTRAP_ADMIN_PASSWORD:-}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --email) email="$2"; shift 2 ;;
      --password) password="$2"; shift 2 ;;
      *) die "admin 不认识的参数: $1" ;;
    esac
  done
  [ -n "$email" ] || die "admin 需要 --email"
  if [ -z "$password" ]; then
    # 不回显;也不落 shell history——这是相对 --password 更推荐的路径
    read -r -s -p "为 $email 设置密码(不少于 12 位,不回显): " password; echo
  fi
  [ "${#password}" -ge 12 ] || die "密码不少于 12 位"

  info "创建租户与管理员 $email(幂等:已存在则原样保留)…"
  BOOTSTRAP_ADMIN_EMAIL="$email" BOOTSTRAP_ADMIN_PASSWORD="$password" \
    docker compose --profile bootstrap run --rm bootstrap-admin
}

# ------------------------------------------------------------
# verify:部署体检
# ------------------------------------------------------------
cmd_verify() {
  require_env
  local backend_port frontend_port fail=0
  backend_port="$(sed -n 's/^BACKEND_HOST_PORT=//p' "$ENV_FILE" | tail -1)"
  frontend_port="$(sed -n 's/^FRONTEND_HOST_PORT=//p' "$ENV_FILE" | tail -1)"
  backend_port="${backend_port:-8000}"
  frontend_port="${frontend_port:-5173}"

  check() { # $1 描述 $2 URL
    if curl -fsS -m 10 -o /dev/null "$2"; then
      info "✓ $1"
    else
      warn "✗ $1  ($2)"
      fail=1
    fi
  }
  # readiness 校验 alembic 版本对齐,比 /health 更严——半建成的库过不了它
  check "后端就绪(schema 已迁移到位)" "http://127.0.0.1:${backend_port}/health/readiness"
  check "Prometheus 指标"              "http://127.0.0.1:${backend_port}/metrics"
  check "前端页面"                     "http://127.0.0.1:${frontend_port}/"

  local prefix unhealthy
  prefix="$(sed -n 's/^COMPOSE_RESOURCE_PREFIX=//p' "$ENV_FILE" | tail -1)"
  # 空前缀会让 grep -F "" 匹配全宿主机——只体检本栈,别替邻居项目报警
  prefix="${prefix:-audiography}"
  unhealthy="$(docker ps --filter "health=unhealthy" --format '{{.Names}}' | grep -F "$prefix" || true)"
  if [ -n "$unhealthy" ]; then
    warn "✗ unhealthy 容器: $unhealthy"
    fail=1
  else
    info "✓ 无 unhealthy 容器"
  fi

  if grep -qE '^(MYSQL_PASSWORD=change-me|JWT_SECRET=change-me)' "$ENV_FILE"; then
    warn "✗ .env 中仍有 change-me 默认密钥——生产环境必须替换(init 生成的 .env 不会出现这种情况)"
    fail=1
  else
    info "✓ 无 change-me 默认密钥"
  fi

  if [ "$fail" -eq 0 ]; then
    info "体检通过"
  else
    die "体检未通过,见上方 ✗ 项"
  fi
}

# ------------------------------------------------------------
# status / logs / down
# ------------------------------------------------------------
with_saved_flags() {
  local profile; profile="$(saved_profile)"
  compose_profiles "${profile:-mock}"
}

# shellcheck disable=SC2086  # $f 是有意展开的 --profile 参数序列
cmd_status() { require_env; local f; f="$(with_saved_flags)"; docker compose $f ps; }
# shellcheck disable=SC2086
cmd_logs()   { require_env; local f; f="$(with_saved_flags)"; docker compose $f logs -f --tail 200 "$@"; }
cmd_down() {
  require_env
  local f; f="$(with_saved_flags)"
  # shellcheck disable=SC2086  # 同上
  # 刻意不提供 -v:mysql_data / working_dir / master_key 三个卷是全部业务数据,
  # 删卷必须是操作者显式敲出的命令,不该藏在脚本参数后面。
  docker compose $f down
  info "容器已停止;数据卷全部保留(删除数据请阅读 docs/deployment.md 的备份章节)"
}

usage() {
  cat <<'EOF'
用法: ./scripts/deploy.sh <命令> [参数]

  init --profile <mock|cpu|gpu|gpu-multi>  生成 .env(随机密钥)并写入 profile 配置
  model                                     下载 Silero VAD 权重(models profile 必需)
  up [profile] [--offline] [--dev-frontend] 启动并等待健康;--offline 禁 build/pull;
                                            --dev-frontend 保留 Vite 开发服务器(默认生产 nginx)
  admin --email <邮箱> [--password <密码>]  创建首个租户与管理员(幂等)
  verify                                    部署体检(就绪/指标/前端/健康/密钥)
  status | logs [service] | down            日常运维

完整部署矩阵与离线(私有化)部署见 docs/deployment.md。
EOF
}

cd "$ROOT"
case "${1:-}" in
  init)   shift; cmd_init "$@" ;;
  model)  shift; cmd_model "$@" ;;
  up)     shift; cmd_up "$@" ;;
  admin)  shift; cmd_admin "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  status) shift; cmd_status "$@" ;;
  logs)   shift; cmd_logs "$@" ;;
  down)   shift; cmd_down "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; die "未知命令: $1" ;;
esac
