# Docker Compose 一键部署

本仓库提供两种可一键启动的部署合同：默认的 SQLite 内嵌模式，以及 PostgreSQL 支撑的
API/Worker 分布式运行模式。两者都使用多阶段构建、非 root 应用进程、只读根文件系统、
存活/就绪检查、资源限制和日志轮转；内嵌模式另带页面加密配置、SQLite 一致性备份和 Caddy
自动 HTTPS，分布式模式则带一次性迁移、可靠任务队列、Worker 横向扩缩和 PostgreSQL 逻辑备份。

选择边界如下：

| 模式 | 状态存储 | 扩缩容 | 配置方式 | 适用场景 |
| --- | --- | --- | --- | --- |
| 内嵌 | SQLite | 一个应用进程 | 环境变量或设置页 | 桌面、个人服务、单机交互 |
| 分布式 | PostgreSQL | API/Worker 分离，Worker 可横向扩容 | 所有实例使用同一份不可变环境配置 | 长任务、多执行实例、故障接管 |

两种模式都只有部署级共享 API Key，不提供用户身份或租户隔离。公网部署必须在前方配置 HTTPS、
身份网关、外部限流和审计。

当前源码的两种模式均默认使用 `blackboard`。新任务须提供 `blackboard.acceptance`，网页须
选择完成条件；CLI 使用 `--blackboard-spec` 或明确选择 `--acceptance-template saved-draft`。
已有环境中的 `WORK_HARNESS_WORKFLOW_ENGINE` 覆盖值继续生效；API 与所有 Worker 应使用相同
配置。需要旧工作流时请求指定 `engine=legacy`。回退时将部署环境设为
`WORK_HARNESS_WORKFLOW_ENGINE=legacy`、`WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS=false`，
再一致重启应用服务。旧任务继续按其原引擎、版本及输入恢复，不转换历史证据。

本次默认切换验证覆盖源码下 SQLite 与真实 PostgreSQL 16 API/Worker；Windows EXE 和容器
恢复尚未重新演练。以下镜像安装说明不表示已发布包含此次切换的新镜像。

## 1. 环境要求

- Docker Engine 24+ 或 Docker Desktop；
- Docker Compose v2.24+；
- 构建时能够访问 npm、PyPI 和容器镜像仓库；
- 公网模式需要一个已经解析到部署主机的域名，以及开放的 TCP 80/443 和 UDP 443。

Docker Desktop + WSL 用户需要先在 Docker Desktop 中启用对应发行版的 WSL
Integration，确保 WSL 内的 `docker info` 可以连接到 Linux Engine。如果 Docker Desktop 已挂载
WSL socket、但没有创建 `/var/run/docker.sock`，仓库内的 `scripts/compose.sh` 会在未显式设置
`DOCKER_HOST` 时自动检测并使用该 socket；它不会修改用户的全局 Docker context。

## 2. 本机一键启动

默认模式只绑定 `127.0.0.1:8000`，以安全的首次配置模式启动，不会启用 Mock Provider：

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

首次进入后，设置页会直接打开“默认模型服务”：填写接口地址、密钥和真实模型名称，选择默认
工作模型并保存，然后执行 `./scripts/compose.sh reload`。在此之前 `/health/ready` 和
`POST /v1/runs` 都返回 503，不会生成模拟的“已完成”结果。

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
WORK_HARNESS_IMAGE=ghcr.io/<owner>/<repository>:0.2.2
```

然后执行：

```bash
./scripts/compose.sh install
```

`install` 只拉取并启动已发布镜像，不依赖本机 npm、PyPI 或源码构建。它会拒绝空镜像名
和默认的 `work-harness:local`，以免误以为已经安装发布版本。后续执行同一命令会先拉取
远端同标签镜像；正式环境仍建议使用不可变版本标签或 digest。

## 3. 配置文件与密钥边界

内嵌部署使用两个不同的环境文件：

| 文件 | 用途 | 是否进入容器 |
| --- | --- | --- |
| 根目录 `.env` | Compose 插值：镜像、端口、域名、运行 profile | 只转发 Compose 明确列出的设置 |
| `deploy/runtime.env` | API Key、模型端点、模型 Key、Provider 自定义变量 | 作为容器运行环境加载 |

`deploy/runtime.env` 已被 Git 和 Docker 构建上下文排除。不要把密钥写入
`compose.yaml`、`config/*.yaml` 或 `VITE_*` 前端构建变量。

分布式部署对应使用 `.env.distributed` 和 `deploy/runtime.distributed.env`。前者只负责 Compose
插值和数据库初始化值，后者注入 API/Worker 的部署 Key 与模型配置；两个实际文件同样被 Git
忽略，只提交带 `.example` 后缀的模板。

Provider 凭据有两个互斥通道：

| 通道 | 配置方式 | 适用场景 |
| --- | --- | --- |
| 环境变量 | Profile 保存变量名，Key 由 `deploy/runtime.env` 或外部 Secret Manager 注入 | 自动化部署、集中密钥轮换 |
| 页面加密保存 | 已认证管理员在 Provider 设置页录入，服务端使用独立密钥加密后持久化 | 交互式单机部署、不方便控制进程环境的场景 |

页面保存的 Key 不会被查询 API 回显，浏览器输入框留空即保留旧值。密文位于
`providers.sqlite`，加密密钥位于同卷的 `providers.sqlite.key`，该文件在 ProviderStore 初始化时
自动创建并限制为 `0600`。这种分离能降低单独泄露数据库文件的风险，但不能抵御整个数据卷、
运行中容器或完整备份同时泄露；因此仍要限制宿主机、Docker 和备份目录的访问。

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

若直接使用内置默认 Provider 的环境变量通道，需要同时设置 `MODEL_BASE_URL`、
`MODEL_API_KEY` 和真实的 `MODEL_ID`。缺少 `MODEL_ID` 时，占位模型不会通过就绪检查。

## 4. 五种运行 profile

| Profile | API 鉴权 | 默认模型 | 用途 |
| --- | --- | --- | --- |
| `setup` | 关闭 | 仅兼容 Provider、等待配置 | 默认首次启动和仅本机交互式配置 |
| `local` | 关闭 | Mock | 显式的源码开发、自动化测试和框架基准 |
| `secure` | 开启 | Mock | 带鉴权的部署演练，或保留 Mock fallback 的单实例接入 |
| `production` | 开启 | 仅兼容 Provider | 已完成真实模型契约和质量验收后的部署 |
| `distributed` | 开启 | 仅兼容 Provider | PostgreSQL API/Worker 可靠队列；不允许 Mock 或页面修改运行配置 |

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

默认 `setup` 和生产 Profile 都可以在模型尚未配置时以“待配置”状态启动，`/health/live` 保持 200，
`/health/ready` 返回 503，前端会阻止提交真实任务并直接打开 Provider 设置。按以下方式任选
一个凭据通道：

浏览器的 `/settings` 是独立设置路由：模型设置管理 Provider；Agent 提示词管理各工作阶段的
行为；运行配置只开放并发、检查、预算和超时等安全字段。Prompt 与运行参数写入数据卷中的
`settings.json`，不修改镜像内的 YAML 或 Markdown。

1. 编辑“默认模型服务”，或添加另一个自定义模型服务；
2. 选择 OpenAI 或 DeepSeek 兼容格式、对话补全或 Responses 请求格式，并填写接口地址；
3. 添加真实模型名称并选择“默认工作模型”；系统会自动分配给规划、执行、检查和汇总阶段，
   需要时再展开高级设置逐阶段或逐成员覆盖；
4. 环境变量方式：选择“由部署环境提供”，保存变量名，把实际值写入
   `deploy/runtime.env` 或 Secret Manager；
5. 页面方式：选择“在本页面加密保存”，输入 Key；保存后页面只显示凭据已经配置；
6. 启用配置并执行 `./scripts/compose.sh reload`，使新路由和凭据装载到运行时；
7. 执行 `./scripts/compose.sh ready`，并查看 `/v1/compatibility` 后再做真实任务验收。

访问宿主机上的模型服务时可使用 `host.docker.internal`。Linux Engine 下 Compose
已经将它映射到 host gateway。HTTP 或私网端点还必须在对应 Provider Profile 中明确
启用 `allow_insecure_http` 和 `allow_private_network`；这些开关不应在公网端点上启用。

`production` 会禁用 Mock 并启用默认 `compatible` Provider。投入真实任务前，必须确保持久化
的 `compatible` Profile 或生产 YAML 已使用真实模型 ID 和完整路由替换
`configured-model`，并且所选凭据通道已就绪。

## PostgreSQL API/Worker deployment

### 首次启动

生成不会进入 Git 的配置文件：

```bash
./scripts/compose.sh init-distributed
```

该命令只在 `.env.distributed` 和 `deploy/runtime.distributed.env` 均不存在时执行，并把文件权限
设为 `0600`。随后必须替换：

- 至少 32 字符且只包含 URL 安全字符的 `WORK_HARNESS_DATABASE_PASSWORD`；
- 至少 32 字符的 `WORK_HARNESS_API_KEY`；
- 真实的 `MODEL_BASE_URL`、`MODEL_API_KEY` 和 `MODEL_ID`；
- 使用发布镜像时，将 `WORK_HARNESS_IMAGE` 改成不可变版本标签或 digest。

同一文件还提供全局队列容量、每 Worker 并发、运行/租约/心跳时间、领取轮询、Worker 失效时间、
最大尝试次数和重试退避。它们会作为同一组环境变量注入所有角色，再由 Pydantic 校验范围及
`heartbeat < lease`、`retry_base <= retry_max` 等交叉条件；不要为单个副本临时改不同值，否则
配置摘要不一致会使 API readiness 失败。

源码构建并启动：

```bash
./scripts/compose.sh distributed
```

使用 GHCR 发布镜像时改为：

```bash
./scripts/compose.sh install-distributed
```

Compose 会依次等待 PostgreSQL、执行 `work-harness migrate`、启动 Worker，再启动 API。默认拓扑
为一个 API、一个 Worker 和一个不映射宿主机端口的 PostgreSQL；API 默认只绑定
`127.0.0.1:8000`。`/health/ready` 只有在真实 Provider 可用、数据库可访问，并且至少一个配置
摘要与 API 一致的 Worker 心跳存活时才返回 200。

### 扩缩、状态和日志

```bash
./scripts/compose.sh scale-workers 3
./scripts/compose.sh distributed-status
./scripts/compose.sh distributed-logs
```

每个 Worker 最多并行执行 `run_max_concurrency` 个完整 Graph Run。等待队列容量由数据库事务
全局限制，不会因增加 API 实例而重复计算。领取使用 `FOR UPDATE SKIP LOCKED`，每次重新领取都
生成新的 fencing token；旧 Worker 不能续租、确认或退回已经被接管的 Job。

单机 Compose 默认只暴露一个 API 入口。若要扩展 API，请在 Kubernetes、Nomad 或外部负载均衡
器中运行同一镜像的多个 `execution.role=api` 实例，并保证它们与 Worker 使用完全相同的配置和
数据库。不要通过在一个 Uvicorn 容器中增加多个进程代替独立实例。

### 重试、取消与死信

基础设施错误按确定性指数退避重试；Worker 崩溃后由租约到期触发接管。达到最大尝试次数后 Job
进入 `dead`，不会无限循环。管理接口为：

```text
GET  /v1/queue/dead
POST /v1/queue/dead/{job_id}/replay
```

列表不返回原始 payload；重放会保留旧死信审计记录，并以当前 Graph revision 创建新 Job。
这些接口当前受同一个部署 API Key 保护，还没有独立管理员角色，不能当成完整的多用户授权。

取消会在数据库事务中使 available/leased Job 失效。运行中的 Worker 最迟在下一次租约心跳时
停止；对外副作用无法由数据库回滚，因此所有会写外部系统的 Tool 必须按
`(run_id, tool_call_id)` 实现幂等。

### 配置与滚动升级

分布式模式暂时禁用 Provider、Prompt 和运行参数的页面写接口，避免不同实例读取 Memory/本地
配置后产生不一致行为。修改模型、Prompt、Skill、Tool 或运行策略时，应更新统一部署配置并滚动
重启全部实例。

每个 Job 都保存应用版本与有效配置计算出的 `graph_revision`，Worker 只领取完全匹配的 Job。
不兼容升级前应先确认 `/health` 中 `shared_queue.available_jobs` 和 `leased_jobs` 都为 0，再替换
镜像；否则旧 revision 的 Job 会留在队列中，必须回滚旧 Worker 排空或实施显式迁移。API
readiness 会在存活 Worker 摘要不一致时返回 503。

### PostgreSQL 备份与恢复

备份命令会停止 API/Worker，完成数据库和配置打包后，仅恢复原先运行的服务：

```bash
./scripts/compose.sh backup-distributed
```

输出为 `backups/work-harness-postgres-YYYYMMDDTHHMMSSZ/`。其中 `database.sql` 包含运行索引、
可靠队列、全部黑板对象版本与事件、附件正文、激活与用量账本、验证凭据和 LangGraph
Checkpoint；`config/`、`prompts/`、`skills/` 来自当前镜像，部署 env 文件来自宿主项目。
如果 API 数据目录存在页面设置或 Provider 数据库及密钥，也一起保留。整个目录限制为当前
用户可读；清单记录文件大小、摘要、应用版本及 Profile。数据库连接口令只由环境传递给
`pg_dump`，不会作为命令参数输出。

先用当前版本验证并解包到一个不存在的目录，再导入**新建的隔离数据库**：

```bash
.venv/bin/python scripts/backup_postgres.py backups/<backup-name> /tmp/work-harness-restored --restore
# PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE 指向事先创建的隔离数据库。
psql -X --set=ON_ERROR_STOP=1 --single-transaction --file=/tmp/work-harness-restored/database.sql
```

解包命令不连接 PostgreSQL，也不覆盖现有目录。恢复时使用原应用镜像、Profile、Prompt、Skill
和环境覆盖值，将恢复目录中的运行资源及可选 `var/` 文件放回相应位置。确认运行列表、黑板
附件与等待任务恢复后再开放流量。版本升级的迁移另行执行，不把“恢复”与“升级”混为一步。

Bundled Compose 使用 PostgreSQL 初始化用户同时执行迁移和运行 DML，并依靠内部网络隔离；更高
安全等级应使用外部 PostgreSQL，拆分 DDL 与 DML 角色、启用 TLS、限制来源网络并配置数据库级
备份和时间点恢复。`WORK_HARNESS_DATABASE_URL` 不应写入前端、YAML 或日志。

停止分布式服务但保留数据：

```bash
./scripts/compose.sh distributed-down
```

不要增加 `-v`，除非已经验证备份且明确要永久删除 PostgreSQL 卷。

## 6. 内嵌模式公网 HTTPS 一键部署

首次生产安装推荐直接生成一组不会进入 Git 的模板：

```bash
./scripts/compose.sh init-production
```

该命令仅在 `.env` 和 `deploy/runtime.env` 都不存在时执行，避免覆盖已有配置。编辑真实域名和
至少 32 字符的 `WORK_HARNESS_API_KEY`；模型变量可以全部留空，启动后再通过页面添加。

先完成以下准备：

1. 域名 A/AAAA 记录指向部署主机；
2. 防火墙开放 TCP 80、TCP 443 和 UDP 443；
3. 根目录 `.env` 使用真实域名并选择 `production`；
4. `deploy/runtime.env` 已设置至少 32 字符的 `WORK_HARNESS_API_KEY`。

示例 `.env`：

```dotenv
WORK_HARNESS_PROFILE=production
WORK_HARNESS_DOMAIN=harness.example.net
WORK_HARNESS_BIND_ADDRESS=127.0.0.1
```

启动应用和 Caddy：

```bash
./scripts/compose.sh production
```

如果 `.env` 已配置发布镜像，则使用：

```bash
./scripts/compose.sh install-production
```

脚本会拒绝示例域名、非 `production` profile、弱部署 Key，以及只设置 URL 或只设置 Key 的
环境变量 Provider。Caddy 自动申请和续期证书，并附加 HSTS、CSP、禁止 MIME 嗅探等响应头；
应用端口仍只绑定到宿主机回环地址，容器间通过 Compose 网络通信。

## 7. 内嵌模式日常运维

```bash
# 状态
./scripts/compose.sh status

# 日志
./scripts/compose.sh logs

# 应用与 Compose 配置检查
./scripts/compose.sh check

# 验证所有启用的真实 Provider 已经就绪
./scripts/compose.sh ready

# 拉取新版基础镜像并重新构建
./scripts/compose.sh update

# 停止服务，保留所有 SQLite 数据和证书
./scripts/compose.sh down
```

`down` 不会删除命名卷。不要执行 `docker compose down -v`，除非已经验证备份并明确
希望永久删除运行、Checkpoint、Provider Profile 和 Caddy 证书数据。

## 8. 内嵌模式 SQLite 备份与恢复

备份命令会停止应用，完成备份后恢复原先的启动状态：

```bash
./scripts/compose.sh backup
```

停止写入者后，通过 SQLite Backup API 依次保存同一静止状态的数据库。运行时持有进程锁，
直接调用备份脚本时若应用仍在使用数据，会拒绝执行。此命令支持 Compose 的标准单目录布局；
自定义数据文件名、跨目录存储或自定义 Settings 路径需要先按实际部署收集完整文件。

```text
backups/work-harness-YYYYMMDDTHHMMSSZ/
  var/
    checkpoints.sqlite
    runs.sqlite
    blackboard.sqlite
    providers.sqlite
    providers.sqlite.key
    settings.json
  config/
  prompts/
  skills/
  .env                         # 源部署存在时保存
  deploy/runtime.env           # 源部署存在时保存
  manifest.json
```

黑板附件正文、对象历史、事件、提交回执、激活与用量账本、验证凭据均在 `blackboard.sqlite`
内。尚未创建过黑板 Run 的部署可没有该数据库；页面设置同样可缺省。Provider 数据库和加密
密钥必须成对备份。备份目录权限为 `0700`，文件为 `0600`，包括可能含凭据的部署 env 文件。

验证清单与数据库完整性，并恢复到一个不存在的隔离目录：

```bash
.venv/bin/python scripts/backup_sqlite.py backups/<backup-name> /tmp/work-harness-restored --restore
```

恢复不会覆盖当前部署。使用原应用版本和 Profile，保持原环境覆盖值；本地相对路径部署可
以恢复目录作为 `--project-root` 运行。容器部署须停机后将恢复的整个 `var/` 集合放入新卷，
将目录所有者设为 `10001:10001`，并使用匹配的镜像资源和部署配置。不要只替换 Checkpoint
或单独替换黑板数据库。绝对路径部署须保留原挂载位置，不能改写历史执行指纹来强行续跑。

回退新请求时同时设置：

```dotenv
WORK_HARNESS_WORKFLOW_ENGINE=legacy
WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS=false
```

这两个开关只控制新 Run：显式新建黑板任务会被拒绝，默认新请求使用旧引擎，已有幂等请求
仍返回原 Run。已有黑板任务继续使用原黑板执行版本接收人工输入和恢复；保留黑板数据库，
不把它们改标为旧引擎。分布式部署在 API 与 Worker 上应用相同配置。

## 9. 构建与发布镜像

发布 GitHub Release 后，`release.yml` 会自动构建双架构镜像、附加 SBOM 与来源证明，
并使用版本号、主次版本号和 `latest` 标签推送到当前仓库的 GHCR。它使用 GitHub 提供的
短期 `GITHUB_TOKEN`，不要求配置容器仓库密码。

自行发布到其他 Registry 时，建议使用不可变版本标签：

```dotenv
WORK_HARNESS_IMAGE=registry.example.com/team/work-harness:0.2.2
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

- `unhealthy`：查看启动日志，通常是配置错误或数据库权限问题；Provider 尚未配置只会影响
  readiness，不会让容器存活检查失败；
- `401`：浏览器中输入与 `deploy/runtime.env` 相同的部署 API Key；
- `ready` 返回 503：补齐所选环境变量，或在页面保存凭据并 `reload`；
- 分布式 `ready` 返回 503：检查 `distributed-status`、Worker 日志，以及 `/health` 中
  `shared_queue_healthy`、`shared_queue.incompatible_active_jobs`、`live_workers` 和
  `worker_config_consistent`；
- Worker 反复重启：检查数据库连通性、schema 是否已迁移，以及所有实例的模型配置是否一致；
- 升级后 Job 不再领取：该 Job 的 `graph_revision` 与新 Worker 不一致；回滚兼容 Worker 排空，
  或按迁移方案显式重放，不能直接改数据库哈希；
- 页面凭据无法解密：确认 `providers.sqlite.key` 与数据库来自同一份备份且容器用户可读；
- 无法连接宿主机模型：使用 `host.docker.internal`，并确认服务监听地址和 Provider 私网策略；
- WSL 报 Docker daemon 不可用：启动 Docker Desktop 并启用该发行版的 WSL Integration；
- 端口 80/443 被占用：停止冲突的代理，或仅在内网模式使用自定义映射端口。
