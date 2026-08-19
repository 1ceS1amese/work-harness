# Docker Compose 一键部署

本仓库提供完整的单实例部署方案：多阶段构建前端和 Python 运行环境、非 root
应用进程、只读根文件系统、SQLite 持久化卷、健康检查、日志轮转、在线一致性备份，
以及可选的 Caddy 自动 HTTPS 网关。

当前 Checkpoint、RunStore 和 ProviderStore 都使用 SQLite，因此部署合同是：

- 一个 `work-harness` 容器；
- 一个持久化数据卷；
- 不增加 Uvicorn worker，不水平扩容；
- 公网部署必须启用 API 鉴权和 HTTPS。

## 1. 环境要求

- Docker Engine 24+ 或 Docker Desktop；
- Docker Compose v2.24+；
- 构建时能够访问 npm、PyPI 和容器镜像仓库；
- 公网模式需要一个已经解析到部署主机的域名，以及开放的 TCP 80/443 和 UDP 443。

Docker Desktop + WSL 用户需要先在 Docker Desktop 中启用对应发行版的 WSL
Integration，确保 WSL 内的 `docker info` 可以连接到 Linux Engine。

## 2. 本机一键启动

默认模式只绑定 `127.0.0.1:8000`，使用 Mock Provider，不需要任何密钥：

```bash
./scripts/compose.sh up
```

脚本会自动完成：

1. 检查 Docker Engine 和 Compose v2；
2. 首次运行时从模板创建权限为 `0600` 的 `deploy/runtime.env`；
3. 校验 Compose；
4. 构建前端和后端镜像；
5. 启动容器并等待 `/health/live` 通过。

浏览器访问：

```text
http://127.0.0.1:8000/
```

如需调整镜像名、监听地址或端口：

```bash
cp .env.example .env
```

然后只修改根目录 `.env` 中的 Compose 控制项。例如局域网监听：

```dotenv
WORK_HARNESS_BIND_ADDRESS=0.0.0.0
WORK_HARNESS_PORT=8000
```

没有防火墙和鉴权时不要使用 `0.0.0.0`。

### 使用 GitHub Release 对应镜像

每次 GitHub Release 会自动向仓库的 GitHub Packages 页面发布 `linux/amd64` 和
`linux/arm64` GHCR 镜像。复制页面上的完整版本化镜像引用，写入 `.env`：

```dotenv
WORK_HARNESS_IMAGE=ghcr.io/<owner>/<repository>:0.2.1
```

然后执行：

```bash
./scripts/compose.sh install
```

`install` 只拉取并启动已发布镜像，不依赖本机 npm、PyPI 或源码构建。它会拒绝空镜像名
和默认的 `work-harness:local`，以免误以为已经安装发布版本。后续执行同一命令会先拉取
远端同标签镜像；正式环境仍建议使用不可变版本标签或 digest。

## 3. 配置文件与密钥边界

部署使用两个不同的环境文件：

| 文件 | 用途 | 是否进入容器 |
| --- | --- | --- |
| 根目录 `.env` | Compose 插值：镜像、端口、域名、运行 profile | 只转发 Compose 明确列出的设置 |
| `deploy/runtime.env` | API Key、模型端点、模型 Key、Provider 自定义变量 | 作为容器运行环境加载 |

`deploy/runtime.env` 已被 Git 和 Docker 构建上下文排除。不要把密钥写入
`compose.yaml`、`config/*.yaml`、Provider Profile 或 `VITE_*` 前端构建变量。

若 Provider Profile 使用自定义变量名，直接追加到 `deploy/runtime.env`：

```dotenv
TEAM_MODEL_BASE_URL=https://models.example.com/v1
TEAM_MODEL_API_KEY=replace-manually
```

然后执行：

```bash
./scripts/compose.sh reload
```

`reload` 会重新创建应用容器，因此会加载新环境变量，并从
`providers.sqlite` 重新装载持久化 Provider Profile。

## 4. 三种运行 profile

| Profile | API 鉴权 | 默认模型 | 用途 |
| --- | --- | --- | --- |
| `local` | 关闭 | Mock | 本机首次部署和框架验证 |
| `secure` | 开启 | Mock | 带鉴权的部署演练，或保留 Mock fallback 的单实例接入 |
| `production` | 开启 | 仅兼容 Provider | 已完成真实模型契约和质量验收后的部署 |

使用 `secure` 或 `production` 时，在根目录 `.env` 设置：

```dotenv
WORK_HARNESS_PROFILE=secure
```

并在 `deploy/runtime.env` 手动设置一个高强度随机值：

```dotenv
WORK_HARNESS_API_KEY=replace-manually
```

鉴权 profile 在 Key 为空时会拒绝启动，避免产生一个表面健康但所有业务接口都无法访问
的容器。

浏览器收到 401 后会打开“部署 API 身份验证”窗口。输入的 Key 仅保存在当前标签页
的 `sessionStorage`；普通请求和使用 Fetch 建立的 SSE 流都会附带 `X-API-Key`。

`/health/live` 仅用于容器存活检查，不需要认证；`/health`、`/v1/*` 和 API 文档
仍受认证配置控制。

这里的鉴权是单个部署共享 Key，不是用户身份、租户隔离或细粒度授权。多人或企业部署
仍应在 Caddy 前接入组织的身份网关，并配置外部限流和审计策略。

## 5. 接入自定义模型 Provider

建议按以下顺序操作，避免因缺少环境变量导致容器启动失败：

1. 以 `local` 或 `secure` 启动；
2. 在 Provider 管理界面创建 Profile，先保持 `enabled: false`；
3. 配置协议方言、`chat_completions`/`responses`、模型白名单、主 Agent 与子 Agent 路由；
4. 将 Profile 引用的 Base URL 和 API Key 环境变量写入 `deploy/runtime.env`；
5. 回到界面启用 Profile；
6. 执行 `./scripts/compose.sh reload`；
7. 检查 `/health` 和 `/v1/compatibility`，再执行真实任务验收。

访问宿主机上的模型服务时可使用 `host.docker.internal`。Linux Engine 下 Compose
已经将它映射到 host gateway。HTTP 或私网端点还必须在对应 Provider Profile 中明确
启用 `allow_insecure_http` 和 `allow_private_network`；这些开关不应在公网端点上启用。

`production` 会禁用 Mock 并启用默认 `compatible` Provider。切换前必须确保持久化的
`compatible` Profile 或生产 YAML 已使用真实模型 ID 和完整路由替换
`configured-model`，且它引用的环境变量已经存在。

## 6. 公网 HTTPS 一键部署

先完成以下准备：

1. 域名 A/AAAA 记录指向部署主机；
2. 防火墙开放 TCP 80、TCP 443 和 UDP 443；
3. 根目录 `.env` 使用真实域名，并选择 `secure` 或 `production`；
4. `deploy/runtime.env` 已设置非空 `WORK_HARNESS_API_KEY`。

示例 `.env`：

```dotenv
WORK_HARNESS_PROFILE=secure
WORK_HARNESS_DOMAIN=harness.example.net
WORK_HARNESS_BIND_ADDRESS=127.0.0.1
```

启动应用和 Caddy：

```bash
./scripts/compose.sh public
```

如果 `.env` 已配置发布镜像，则使用：

```bash
./scripts/compose.sh install-public
```

脚本会拒绝示例域名、`local` profile 或空 API Key。Caddy 自动申请和续期证书，
应用端口仍只绑定到宿主机回环地址，容器间通过 Compose 网络通信。

## 7. 日常运维

```bash
# 状态
./scripts/compose.sh status

# 日志
./scripts/compose.sh logs

# 应用与 Compose 配置检查
./scripts/compose.sh check

# 拉取新版基础镜像并重新构建
./scripts/compose.sh update

# 停止服务，保留所有 SQLite 数据和证书
./scripts/compose.sh down
```

`down` 不会删除命名卷。不要执行 `docker compose down -v`，除非已经验证备份并明确
希望永久删除运行、Checkpoint、Provider Profile 和 Caddy 证书数据。

## 8. 备份与恢复

在线备份：

```bash
./scripts/compose.sh backup
```

脚本使用 SQLite Backup API 获得一致性快照，不直接复制正在写入的 WAL 数据库。
输出保存在：

```text
backups/work-harness-YYYYMMDDTHHMMSSZ/
  checkpoints.sqlite
  runs.sqlite
  providers.sqlite
  manifest.json
```

`manifest.json` 记录每个文件的大小和 SHA-256。恢复会覆盖当前数据，因此没有放入
一键脚本。恢复前应：

1. 再执行一次当前状态备份；
2. `./scripts/compose.sh down`，确保没有写入者；
3. 校验目标备份的 `manifest.json`；
4. 将三个 SQLite 文件恢复到 `work-harness-data` 卷根目录；
5. 保持文件所有者为容器用户 UID/GID `10001:10001`；
6. 重新启动并检查运行列表、Provider Profile 和 `/health`。

不要只恢复 Checkpoint 或只恢复 RunStore；二者描述的是同一批运行的不同持久化视图。

## 9. 构建与发布镜像

发布 GitHub Release 后，`release.yml` 会自动构建双架构镜像、附加 SBOM 与来源证明，
并使用版本号、主次版本号和 `latest` 标签推送到当前仓库的 GHCR。它使用 GitHub 提供的
短期 `GITHUB_TOKEN`，不要求配置容器仓库密码。

自行发布到其他 Registry 时，建议使用不可变版本标签：

```dotenv
WORK_HARNESS_IMAGE=registry.example.com/team/work-harness:0.2.1
```

构建后推送：

```bash
docker compose build
docker compose push work-harness
```

镜像包含 `config/`、`prompts/`、`skills/`、`schemas/` 和构建后的前端；不包含
`.env`、`deploy/runtime.env`、本地 SQLite、测试缓存或 `node_modules`。依赖安装严格使用
`uv.lock` 和 `package-lock.json`。

## 10. 故障排查

```bash
docker compose ps
docker compose logs --tail 200 work-harness
docker compose exec work-harness work-harness --project-root /app check-config
curl http://127.0.0.1:8000/health/live
```

常见原因：

- `unhealthy`：查看启动日志，通常是配置、数据库权限或已启用 Provider 缺少环境变量；
- `401`：浏览器中输入与 `deploy/runtime.env` 相同的部署 API Key；
- Provider 启用后启动失败：先补齐它引用的环境变量，再 `reload`；
- 无法连接宿主机模型：使用 `host.docker.internal`，并确认服务监听地址和 Provider 私网策略；
- WSL 报 Docker daemon 不可用：启动 Docker Desktop 并启用该发行版的 WSL Integration；
- 端口 80/443 被占用：停止冲突的代理，或仅在内网模式使用自定义映射端口。
