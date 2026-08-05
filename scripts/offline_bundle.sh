#!/usr/bin/env bash
# ============================================================
# AudioGraphy 离线部署打包 / 导入(私有化内网环境)
#
# 目标机不出网时,在一台有网机器上:
#   ./scripts/offline_bundle.sh export --profile cpu --output /tmp/ag-bundle
#   # 可选:该机器完整启动并跑通过一次后,连模型缓存一起带走
#   ./scripts/offline_bundle.sh export --profile cpu --output /tmp/ag-bundle --with-caches
#
# 把 bundle 目录与代码仓库一起拷到目标机,然后:
#   ./scripts/offline_bundle.sh import --input /path/ag-bundle
#   ./scripts/deploy.sh init --profile cpu
#   ./scripts/deploy.sh up --offline
#
# 镜像清单不在脚本里手抄:直接问 compose 要(config --images),
# 加服务、换 tag 都不需要改这里。
# ============================================================
set -euo pipefail

ROOT="${AG_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# 私有化交付一律生产前端:样式与资产在镜像内,不依赖开发服务器。
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.frontend-prod.yml"
MODEL_FILE="$ROOT/models/silero_vad.onnx"
PREFIX_DEFAULT="audiography"

info() { printf '\033[1;34m[bundle]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bundle]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bundle]\033[0m %s\n' "$*" >&2; exit 1; }

compose_profiles() {
  case "$1" in
    mock)      echo "--profile mock" ;;
    cpu)       echo "--profile models-cpu --profile models-cpu-llm" ;;
    gpu)       echo "--profile models-single-gpu" ;;
    gpu-multi) echo "--profile models-multi-gpu" ;;
    *) die "未知 profile: $1(可选 mock | cpu | gpu | gpu-multi)" ;;
  esac
}

resource_prefix() {
  local p=""
  [ -f "$ROOT/.env" ] && p="$(sed -n 's/^COMPOSE_RESOURCE_PREFIX=//p' "$ROOT/.env" | tail -1)"
  echo "${p:-$PREFIX_DEFAULT}"
}

# 模型缓存卷(compose volumes 一节的 M4-M7 组)。业务数据卷刻意不在列:
# mysql_data / working_dir / master_key 属于备份,不属于装机介质。
CACHE_VOLUMES="ollama_models vllm_cache tei_cache funasr_cache clap_cache campplus_cache"

cmd_export() {
  local profile="" out="" with_caches=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) profile="$2"; shift 2 ;;
      --output) out="$2"; shift 2 ;;
      --with-caches) with_caches=1; shift ;;
      *) die "export 不认识的参数: $1" ;;
    esac
  done
  [ -n "$profile" ] && [ -n "$out" ] || die "export 需要 --profile 与 --output"
  local flags; flags="$(compose_profiles "$profile")"
  mkdir -p "$out"

  info "构建本仓库自有镜像(backend/frontend/funasr/campplus/silero-vad 等)…"
  # 慢速链路可加速: PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple(各 Dockerfile 均支持)
  # shellcheck disable=SC2086
  (cd "$ROOT" && docker compose $COMPOSE_FILES $flags build)

  info "解析 profile=$profile 的完整镜像清单…"
  local images
  # shellcheck disable=SC2086
  images="$(cd "$ROOT" && docker compose $COMPOSE_FILES $flags config --images | sort -u)"
  echo "$images" | sed 's/^/  /'

  info "拉取第三方镜像(mysql/vllm/tei 等,已存在则跳过)…"
  local img
  for img in $images; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  done

  info "docker save → $out/images.tar(镜像层去重后一次成档)…"
  # shellcheck disable=SC2086
  docker save -o "$out/images.tar" $images

  if [ -f "$MODEL_FILE" ]; then
    cp "$MODEL_FILE" "$out/silero_vad.onnx"
    info "已附带 VAD 权重"
  elif [ "$profile" != "mock" ]; then
    warn "models/silero_vad.onnx 不存在——先跑 ./scripts/deploy.sh model,否则目标机 VAD 起不来"
  fi

  if [ "$with_caches" -eq 1 ]; then
    local prefix vol full
    prefix="$(resource_prefix)"
    mkdir -p "$out/caches"
    for vol in $CACHE_VOLUMES; do
      full="${prefix}_${vol}"
      if docker volume inspect "$full" >/dev/null 2>&1; then
        info "导出模型缓存卷 $full …"
        docker run --rm -v "$full":/src:ro -v "$(cd "$out/caches" && pwd)":/out alpine \
          tar czf "/out/${vol}.tar.gz" -C /src .
      else
        # 只在本机真实启动过对应 profile 才会有;导出为空是常见且合法的状态
        warn "卷 $full 不存在,跳过(该服务在本机从未启动过)"
      fi
    done
  else
    warn "未加 --with-caches:目标机首启仍需下载模型权重(可达 10 GB)。真离线环境请在本机完整启动一次后加该参数重导。"
  fi

  {
    echo "profile=$profile"
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "with_caches=$with_caches"
    echo "images:"
    echo "$images" | sed 's/^/  - /'
  } >"$out/MANIFEST"
  info "完成。目标机步骤: import → deploy.sh init --profile $profile → deploy.sh up --offline"
}

cmd_import() {
  local in=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --input) in="$2"; shift 2 ;;
      *) die "import 不认识的参数: $1" ;;
    esac
  done
  [ -n "$in" ] && [ -f "$in/images.tar" ] || die "import 需要 --input <bundle 目录>(含 images.tar)"

  info "docker load(镜像较大,耐心等待)…"
  docker load -i "$in/images.tar"

  if [ -f "$in/silero_vad.onnx" ]; then
    mkdir -p "$ROOT/models"
    cp "$in/silero_vad.onnx" "$MODEL_FILE"
    info "VAD 权重已就位: $MODEL_FILE"
  fi

  if [ -d "$in/caches" ]; then
    local prefix f vol full
    prefix="$(resource_prefix)"
    for f in "$in/caches"/*.tar.gz; do
      [ -e "$f" ] || continue
      vol="$(basename "$f" .tar.gz)"
      full="${prefix}_${vol}"
      if docker volume inspect "$full" >/dev/null 2>&1; then
        # 已有缓存不覆盖:里面可能是目标机自己热起来的更新版本
        warn "卷 $full 已存在,跳过导入(如需覆盖先 docker volume rm)"
        continue
      fi
      info "恢复模型缓存卷 $full …"
      docker volume create "$full" >/dev/null
      docker run --rm -v "$full":/dst -v "$(cd "$in/caches" && pwd)":/in:ro alpine \
        tar xzf "/in/${vol}.tar.gz" -C /dst
    done
  fi

  local manifest_profile=""
  [ -f "$in/MANIFEST" ] && manifest_profile="$(sed -n 's/^profile=//p' "$in/MANIFEST")"
  info "导入完成。下一步:"
  info "  ./scripts/deploy.sh init --profile ${manifest_profile:-<profile>}"
  info "  ./scripts/deploy.sh up --offline"
}

usage() {
  cat <<'EOF'
用法: ./scripts/offline_bundle.sh <命令> [参数]

  export --profile <mock|cpu|gpu|gpu-multi> --output <目录> [--with-caches]
      在有网机器上构建并导出该 profile 的全部镜像(+VAD 权重,可选模型缓存卷)
  import --input <目录>
      在目标机导入镜像/权重/缓存卷;之后 deploy.sh init + up --offline

详见 docs/deployment.md 的「离线 / 内网(私有化)部署」章节。
EOF
}

cd "$ROOT"
case "${1:-}" in
  export) shift; cmd_export "$@" ;;
  import) shift; cmd_import "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; die "未知命令: $1" ;;
esac
