#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNTIME_ENV="$PROJECT_ROOT/deploy/runtime.env"
COMPOSE_ENV="$PROJECT_ROOT/.env"
DISTRIBUTED_ENV="$PROJECT_ROOT/.env.distributed"
DISTRIBUTED_RUNTIME_ENV="$PROJECT_ROOT/deploy/runtime.distributed.env"

configure_docker_host() {
  DESKTOP_WSL_SOCKET=/mnt/wsl/docker-desktop/shared-sockets/host-services/docker.proxy.sock
  if [ -z "${DOCKER_HOST:-}" ] && \
     [ ! -S /var/run/docker.sock ] && \
     [ -S "$DESKTOP_WSL_SOCKET" ]; then
    DOCKER_HOST="unix://$DESKTOP_WSL_SOCKET"
    export DOCKER_HOST
  fi
}

configure_docker_host

compose() {
  docker compose --project-directory "$PROJECT_ROOT" -f "$PROJECT_ROOT/compose.yaml" "$@"
}

compose_distributed() {
  docker compose \
    --project-directory "$PROJECT_ROOT" \
    --env-file "$DISTRIBUTED_ENV" \
    -f "$PROJECT_ROOT/compose.yaml" \
    -f "$PROJECT_ROOT/compose.distributed.yaml" \
    "$@"
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未找到 Docker，请先安装 Docker Engine 或启用 Docker Desktop 集成。" >&2
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "错误：未找到 Docker Compose v2。" >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "错误：Docker daemon 当前不可用。" >&2
    exit 1
  fi
}

prepare_runtime_env() {
  if [ ! -f "$RUNTIME_ENV" ]; then
    cp "$PROJECT_ROOT/deploy/runtime.env.example" "$RUNTIME_ENV"
    chmod 600 "$RUNTIME_ENV"
    echo "已创建 deploy/runtime.env；请启动后在网页中完成模型服务配置。"
  fi
}

file_value() {
  FILE=$1
  KEY=$2
  if [ -f "$FILE" ]; then
    sed -n "s/^${KEY}=//p" "$FILE" | tail -n 1
  fi
}

is_placeholder() {
  case "$1" in
    replace-*|change-*|example-*|changeme|change-me|placeholder|your-*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_public_config() {
  PROFILE=${WORK_HARNESS_PROFILE:-$(file_value "$PROJECT_ROOT/.env" WORK_HARNESS_PROFILE)}
  DOMAIN=${WORK_HARNESS_DOMAIN:-$(file_value "$PROJECT_ROOT/.env" WORK_HARNESS_DOMAIN)}
  API_KEY=$(file_value "$RUNTIME_ENV" WORK_HARNESS_API_KEY)

  case "$PROFILE" in
    secure|production) ;;
    *)
      echo "错误：公网部署必须在 .env 中设置 WORK_HARNESS_PROFILE=secure 或 production。" >&2
      exit 1
      ;;
  esac
  case "$DOMAIN" in
    ""|localhost|harness.example.com)
      echo "错误：公网部署必须在 .env 中设置已经解析到本机的真实域名。" >&2
      exit 1
      ;;
  esac
  if [ ${#API_KEY} -lt 32 ] || is_placeholder "$API_KEY"; then
    echo "错误：公网部署必须设置至少 32 字符且不是模板占位值的 WORK_HARNESS_API_KEY。" >&2
    exit 1
  fi
}

validate_production_config() {
  PROFILE=${WORK_HARNESS_PROFILE:-$(file_value "$COMPOSE_ENV" WORK_HARNESS_PROFILE)}
  API_KEY=$(file_value "$RUNTIME_ENV" WORK_HARNESS_API_KEY)
  MODEL_BASE_URL=$(file_value "$RUNTIME_ENV" MODEL_BASE_URL)
  MODEL_API_KEY=$(file_value "$RUNTIME_ENV" MODEL_API_KEY)
  MODEL_ID=$(file_value "$RUNTIME_ENV" MODEL_ID)

  if [ "$PROFILE" != "production" ]; then
    echo "错误：真实部署必须设置 WORK_HARNESS_PROFILE=production。" >&2
    exit 1
  fi
  if [ ${#API_KEY} -lt 32 ] || is_placeholder "$API_KEY"; then
    echo "错误：WORK_HARNESS_API_KEY 至少需要 32 个字符。" >&2
    exit 1
  fi
  if [ -n "$MODEL_BASE_URL$MODEL_API_KEY$MODEL_ID" ] && \
     { [ -z "$MODEL_BASE_URL" ] || [ -z "$MODEL_API_KEY" ] || [ -z "$MODEL_ID" ]; }; then
    echo "错误：环境变量 Provider 必须同时设置 MODEL_BASE_URL、MODEL_API_KEY 和 MODEL_ID。" >&2
    exit 1
  fi
  if [ -n "$MODEL_API_KEY$MODEL_ID" ] && \
     { is_placeholder "$MODEL_API_KEY" || is_placeholder "$MODEL_ID"; }; then
    echo "错误：MODEL_API_KEY 或 MODEL_ID 仍是模板占位值。" >&2
    exit 1
  fi
  case "$MODEL_BASE_URL" in
    *example.com*|*example.test*)
      echo "错误：MODEL_BASE_URL 仍是示例地址；留空改用页面配置，或填写真实端点。" >&2
      exit 1
      ;;
  esac
}

init_production() {
  if [ -e "$COMPOSE_ENV" ] || [ -e "$RUNTIME_ENV" ]; then
    echo "错误：.env 或 deploy/runtime.env 已存在，为避免覆盖请先手动确认。" >&2
    exit 1
  fi
  cp "$PROJECT_ROOT/.env.production.example" "$COMPOSE_ENV"
  cp "$PROJECT_ROOT/deploy/runtime.production.env.example" "$RUNTIME_ENV"
  chmod 600 "$COMPOSE_ENV" "$RUNTIME_ENV"
  echo "已创建生产配置模板。请填写真实域名和至少 32 字符的部署 API Key。"
  echo "模型服务可稍后在网页的“模型服务与成员分配”中添加。"
  echo "使用发布镜像时，还需把 WORK_HARNESS_IMAGE 改为不可变的 GHCR 版本标签。"
}

init_distributed() {
  if [ -e "$DISTRIBUTED_ENV" ] || [ -e "$DISTRIBUTED_RUNTIME_ENV" ]; then
    echo "错误：.env.distributed 或 deploy/runtime.distributed.env 已存在，为避免覆盖请先手动确认。" >&2
    exit 1
  fi
  cp "$PROJECT_ROOT/.env.distributed.example" "$DISTRIBUTED_ENV"
  cp "$PROJECT_ROOT/deploy/runtime.distributed.env.example" "$DISTRIBUTED_RUNTIME_ENV"
  chmod 600 "$DISTRIBUTED_ENV" "$DISTRIBUTED_RUNTIME_ENV"
  echo "已创建多实例配置模板。请填写数据库密码、API Key 和真实模型配置。"
}

validate_distributed_config() {
  if [ ! -f "$DISTRIBUTED_ENV" ] || [ ! -f "$DISTRIBUTED_RUNTIME_ENV" ]; then
    echo "错误：请先执行 ./scripts/compose.sh init-distributed。" >&2
    exit 1
  fi
  DATABASE_PASSWORD=$(file_value "$DISTRIBUTED_ENV" WORK_HARNESS_DATABASE_PASSWORD)
  API_KEY=$(file_value "$DISTRIBUTED_RUNTIME_ENV" WORK_HARNESS_API_KEY)
  MODEL_BASE_URL=$(file_value "$DISTRIBUTED_RUNTIME_ENV" MODEL_BASE_URL)
  MODEL_API_KEY=$(file_value "$DISTRIBUTED_RUNTIME_ENV" MODEL_API_KEY)
  MODEL_ID=$(file_value "$DISTRIBUTED_RUNTIME_ENV" MODEL_ID)
  if [ ${#DATABASE_PASSWORD} -lt 32 ] || is_placeholder "$DATABASE_PASSWORD"; then
    echo "错误：多实例数据库密码至少需要 32 个字符且不能使用模板占位值。" >&2
    exit 1
  fi
  case "$DATABASE_PASSWORD" in
    *[!A-Za-z0-9._~-]*)
      echo "错误：数据库密码只能使用 URL 安全字符 A-Z、a-z、0-9、点、下划线、波浪线和连字符。" >&2
      exit 1
      ;;
  esac
  if [ ${#API_KEY} -lt 32 ] || is_placeholder "$API_KEY"; then
    echo "错误：WORK_HARNESS_API_KEY 至少需要 32 个字符且不能使用模板占位值。" >&2
    exit 1
  fi
  if [ -z "$MODEL_BASE_URL" ] || [ -z "$MODEL_API_KEY" ] || [ -z "$MODEL_ID" ]; then
    echo "错误：多实例模式必须提供一致的 MODEL_BASE_URL、MODEL_API_KEY 和 MODEL_ID。" >&2
    exit 1
  fi
  if is_placeholder "$MODEL_API_KEY" || is_placeholder "$MODEL_ID"; then
    echo "错误：MODEL_API_KEY 或 MODEL_ID 仍是模板占位值。" >&2
    exit 1
  fi
  case "$MODEL_BASE_URL" in
    *example.com*|*example.test*)
      echo "错误：MODEL_BASE_URL 仍是示例地址。" >&2
      exit 1
      ;;
  esac
}

validate_release_image() {
  IMAGE=${WORK_HARNESS_IMAGE:-$(file_value "$PROJECT_ROOT/.env" WORK_HARNESS_IMAGE)}
  case "$IMAGE" in
    ""|work-harness:local|work-harness:production-local)
      echo "错误：安装发布镜像前，请在 .env 或当前环境设置版本化 WORK_HARNESS_IMAGE。" >&2
      exit 1
      ;;
  esac
}

validate_distributed_release_image() {
  IMAGE=${WORK_HARNESS_IMAGE:-$(file_value "$DISTRIBUTED_ENV" WORK_HARNESS_IMAGE)}
  case "$IMAGE" in
    ""|work-harness:local|work-harness:distributed-local)
      echo "错误：安装发布镜像前，请在 .env.distributed 中设置不可变版本标签。" >&2
      exit 1
      ;;
  esac
}

usage() {
  cat <<'EOF'
用法：./scripts/compose.sh <命令>

命令：
  up       构建镜像并等待服务健康（一键部署）
  install  拉取 WORK_HARNESS_IMAGE 并等待服务健康（不进行本地构建）
  public   同时启动 Caddy，为已解析的域名自动配置 HTTPS
  install-public  使用发布镜像并同时启动 Caddy HTTPS
  init-production  创建不会覆盖现有文件的生产配置模板
  init-distributed 创建 PostgreSQL + API/Worker 多实例配置模板
  production  使用当前源码启动真实 Provider + Caddy HTTPS
  install-production  使用发布镜像启动真实 Provider + Caddy HTTPS
  distributed     构建并启动 PostgreSQL、迁移、API 和 Worker
  install-distributed 使用发布镜像启动多实例架构
  scale-workers N 把执行 Worker 横向扩缩到 N 个实例
  distributed-status 显示多实例服务状态
  distributed-logs 持续查看 API、Worker 与 PostgreSQL 日志
  backup-distributed 暂停 API/Worker，备份 PostgreSQL 与运行配置，随后恢复启动
  distributed-down 停止多实例服务并保留 PostgreSQL 卷
  update   拉取基础镜像、重新构建并替换单实例容器
  reload   重新创建容器，加载最新环境变量和 Provider Profile
  check    校验 Compose 与应用配置
  ready    检查模型 Provider 是否已经可接收真实任务
  status   显示容器状态
  logs     持续查看日志
  backup   暂停应用，备份 SQLite、密钥及运行配置到 backups/，随后恢复启动
  down     停止服务并保留持久化卷
EOF
}

COMMAND=${1:-up}

case "$COMMAND" in
  up)
    require_docker
    prepare_runtime_env
    compose config --quiet
    compose up --detach --build --wait --wait-timeout 180
    compose ps
    ;;
  install)
    require_docker
    prepare_runtime_env
    validate_release_image
    compose config --quiet
    compose pull work-harness
    compose up --detach --no-build --wait --wait-timeout 180
    compose ps
    ;;
  public)
    require_docker
    prepare_runtime_env
    validate_public_config
    compose --profile public config --quiet
    compose --profile public up --detach --build --wait --wait-timeout 180
    compose --profile public ps
    ;;
  install-public)
    require_docker
    prepare_runtime_env
    validate_release_image
    validate_public_config
    compose --profile public config --quiet
    compose --profile public pull
    compose --profile public up --detach --no-build --wait --wait-timeout 180
    compose --profile public ps
    ;;
  init-production)
    init_production
    ;;
  init-distributed)
    init_distributed
    ;;
  production)
    require_docker
    prepare_runtime_env
    validate_production_config
    validate_public_config
    compose --profile public config --quiet
    compose --profile public up --detach --build --wait --wait-timeout 180
    compose --profile public ps
    ;;
  install-production)
    require_docker
    prepare_runtime_env
    validate_release_image
    validate_production_config
    validate_public_config
    compose --profile public config --quiet
    compose --profile public pull
    compose --profile public up --detach --no-build --wait --wait-timeout 180
    compose --profile public ps
    ;;
  distributed)
    require_docker
    validate_distributed_config
    compose_distributed config --quiet
    compose_distributed up --detach --build --wait --wait-timeout 300
    compose_distributed ps
    ;;
  install-distributed)
    require_docker
    validate_distributed_config
    validate_distributed_release_image
    compose_distributed config --quiet
    compose_distributed pull
    compose_distributed up --detach --no-build --wait --wait-timeout 300
    compose_distributed ps
    ;;
  scale-workers)
    require_docker
    validate_distributed_config
    COUNT=${2:-2}
    case "$COUNT" in
      *[!0-9]*|0|"")
        echo "错误：Worker 数量必须是正整数。" >&2
        exit 1
        ;;
    esac
    compose_distributed up --detach --scale "work-harness-worker=$COUNT" --wait
    compose_distributed ps work-harness-worker
    ;;
  distributed-status)
    require_docker
    validate_distributed_config
    compose_distributed ps
    ;;
  distributed-logs)
    require_docker
    validate_distributed_config
    compose_distributed logs --follow --tail 200 work-harness work-harness-worker postgres
    ;;
  backup-distributed)
    require_docker
    validate_distributed_config
    ARCHIVE="work-harness-postgres-$(date -u +%Y%m%dT%H%M%SZ)"
    umask 077
    mkdir -p "$PROJECT_ROOT/backups"
    DUMP=$(mktemp "$PROJECT_ROOT/backups/.postgres-dump.XXXXXX")
    API_RUNNING=false
    WORKERS_RUNNING=false
    if [ -n "$(compose_distributed ps --status running -q work-harness)" ]; then
      API_RUNNING=true
    fi
    if [ -n "$(compose_distributed ps --status running -q work-harness-worker)" ]; then
      WORKERS_RUNNING=true
    fi
    trap 'rm -f "$DUMP"; if [ "$WORKERS_RUNNING" = true ]; then compose_distributed start work-harness-worker; fi; if [ "$API_RUNNING" = true ]; then compose_distributed start work-harness; fi' 0
    compose_distributed stop work-harness work-harness-worker
    compose_distributed exec -T postgres sh -c \
      'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --no-owner --no-privileges --username="$POSTGRES_USER" "$POSTGRES_DB"' \
      > "$DUMP"
    compose_distributed run --rm --no-deps -T --user 0:0 \
      --volume "$PROJECT_ROOT/backups:/backup" --volume "$PROJECT_ROOT:/deployment:ro" \
      --entrypoint sh work-harness -c \
      'python /app/scripts/backup_postgres.py "/backup/$1" "/backup/$2" --local-data /app/var --deployment-root /deployment && chown -R "$3:$4" "/backup/$2"' \
      backup "$(basename "$DUMP")" "$ARCHIVE" "$(id -u)" "$(id -g)"
    echo "备份完成：$PROJECT_ROOT/backups/$ARCHIVE"
    ;;
  distributed-down)
    require_docker
    validate_distributed_config
    compose_distributed down
    ;;
  update)
    require_docker
    prepare_runtime_env
    compose build --pull
    compose up --detach --wait --wait-timeout 180
    compose ps
    ;;
  reload)
    require_docker
    prepare_runtime_env
    compose up --detach --force-recreate --wait --wait-timeout 180
    compose ps
    ;;
  check)
    require_docker
    prepare_runtime_env
    compose config --quiet
    compose run --rm --no-deps work-harness check-config
    ;;
  ready)
    require_docker
    compose exec -T work-harness python -c \
      "import json, os, urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/health/ready', headers={'X-API-Key': os.getenv('WORK_HARNESS_API_KEY', '')}); print(json.load(urllib.request.urlopen(request, timeout=5)))"
    ;;
  status)
    require_docker
    compose ps
    ;;
  logs)
    require_docker
    compose logs --follow --tail 200 work-harness
    ;;
  backup)
    require_docker
    ARCHIVE="work-harness-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$PROJECT_ROOT/backups"
    WAS_RUNNING=false
    if [ -n "$(compose ps --status running -q work-harness)" ]; then
      WAS_RUNNING=true
    fi
    trap 'if [ "$WAS_RUNNING" = true ]; then compose start work-harness; fi' 0
    compose stop work-harness
    compose run --rm --no-deps -T --user 0:0 \
      --volume "$PROJECT_ROOT/backups:/backup" --volume "$PROJECT_ROOT:/deployment:ro" \
      --entrypoint sh work-harness -c \
      'python /app/scripts/backup_sqlite.py /app/var "/backup/$1" --deployment-root /deployment && chown -R "$2:$3" "/backup/$1"' \
      backup "$ARCHIVE" "$(id -u)" "$(id -g)"
    echo "备份完成：$PROJECT_ROOT/backups/$ARCHIVE"
    ;;
  down)
    require_docker
    compose down
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
