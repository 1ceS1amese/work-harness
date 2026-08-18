#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNTIME_ENV="$PROJECT_ROOT/deploy/runtime.env"

compose() {
  docker compose --project-directory "$PROJECT_ROOT" -f "$PROJECT_ROOT/compose.yaml" "$@"
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
    echo "已创建 deploy/runtime.env；默认 Mock 模式无需填写密钥。"
  fi
}

file_value() {
  FILE=$1
  KEY=$2
  if [ -f "$FILE" ]; then
    sed -n "s/^${KEY}=//p" "$FILE" | tail -n 1
  fi
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
  if [ -z "$API_KEY" ]; then
    echo "错误：公网部署必须在 deploy/runtime.env 中设置 WORK_HARNESS_API_KEY。" >&2
    exit 1
  fi
}

validate_release_image() {
  IMAGE=${WORK_HARNESS_IMAGE:-$(file_value "$PROJECT_ROOT/.env" WORK_HARNESS_IMAGE)}
  case "$IMAGE" in
    ""|work-harness:local)
      echo "错误：安装发布镜像前，请在 .env 或当前环境设置版本化 WORK_HARNESS_IMAGE。" >&2
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
  update   拉取基础镜像、重新构建并替换单实例容器
  reload   重新创建容器，加载最新环境变量和 Provider Profile
  check    校验 Compose 与应用配置
  status   显示容器状态
  logs     持续查看日志
  backup   在线备份三个 SQLite 数据库到 backups/
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
    compose exec -T work-harness \
      python /app/scripts/backup_sqlite.py /app/var "/tmp/$ARCHIVE"
    compose cp "work-harness:/tmp/$ARCHIVE" "$PROJECT_ROOT/backups/$ARCHIVE"
    chmod -R go-rwx "$PROJECT_ROOT/backups/$ARCHIVE"
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
