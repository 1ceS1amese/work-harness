# 虚拟机开发环境接续指南

本指南对应源码开发包。默认压缩包不包含 `.env`、`.venv`、`node_modules`、缓存和 `var/` 数据库，避免迁移本机密钥、绝对路径和运行数据。

## 环境要求

- Python 3.11 或更高版本；
- `uv`；
- Node.js 20.19+ 或 22.12+；
- npm 10+；
- Windows PowerShell 7，或常见 Linux Shell。

## 传输方式

任选一种方式把 ZIP 文件传入虚拟机：

1. VMware / VirtualBox 共享目录；
2. 宿主机与虚拟机之间拖放文件；
3. Windows 共享目录；
4. SSH：`scp work-harness-dev-bundle-20260818.zip user@vm:/path/`。

不要通过聊天工具传输包含 `.env` 或 SQLite 运行数据的压缩包。

## 校验压缩包

Windows：

```powershell
Get-FileHash .\work-harness-dev-bundle-20260818.zip -Algorithm SHA256
```

Linux：

```bash
sha256sum work-harness-dev-bundle-20260818.zip
```

结果应与压缩包旁边的 `.sha256` 文件一致。

## 解压

Windows：

```powershell
Expand-Archive .\work-harness-dev-bundle-20260818.zip -DestinationPath C:\dev
cd C:\dev\work-harness
```

Linux：

```bash
unzip work-harness-dev-bundle-20260818.zip -d ~/dev
cd ~/dev/work-harness
```

## 安装依赖

后端：

```powershell
uv sync --extra dev --frozen
uv run work-harness check-config
uv run pytest
```

前端：

```powershell
cd frontend
npm ci
npm run type-check
npm run test -- --run
npm run build
cd ..
```

Linux 下命令相同。

## 启动开发环境

终端一启动 API：

```powershell
uv run work-harness serve
```

终端二启动前端热更新：

```powershell
cd frontend
npm run dev
```

在虚拟机内部访问：

- 前端：`http://127.0.0.1:5173/`；
- API：`http://127.0.0.1:8000/`；
- API 文档：`http://127.0.0.1:8000/docs`。

如果需要从宿主机访问虚拟机服务，可使用虚拟机 NAT 端口转发或 Host-only 网络。只有在网络已隔离并配置鉴权、防火墙后，才把服务绑定到 `0.0.0.0`：

```powershell
uv run work-harness serve --host 0.0.0.0 --port 8000
```

Vite 开发服务器当前默认只绑定 `127.0.0.1`。跨机器访问前端时，应在虚拟机中使用生产构建由 FastAPI 托管，或在确认网络边界后调整 Vite host。

## 配置本地环境变量

从示例复制一个只存在于虚拟机的 `.env`，不要提交或再次打包：

```powershell
Copy-Item .env.example .env
```

项目不会自动把 `.env` 中的密钥写入前端。配置真实模型时，按虚拟机的安全方式设置 `MODEL_BASE_URL` 和 `MODEL_API_KEY`。

## 持久化数据

首次启动会自动创建：

```text
var/checkpoints.sqlite
var/runs.sqlite
var/providers.sqlite
```

默认开发包不携带宿主机数据。如果确实需要继续宿主机里的运行和人工审批状态：

1. 先停止宿主机和虚拟机中的 Work Harness；
2. 将宿主机 `var/checkpoints.sqlite`、`var/runs.sqlite` 与 `var/providers.sqlite` 单独复制到虚拟机的 `var/`；
3. 不要在数据库仍被进程写入时复制；
4. 将数据库按敏感运行数据处理，不通过公开渠道传输；
5. 启动后通过 `GET /v1/runs` 和目标 `GET /v1/runs/{run_id}` 验证。

如果数据库旁存在 `-wal` 或 `-shm` 文件，应在服务停止后确认数据已经合并，或使用 SQLite 备份机制，不要只复制正在写入的主文件。

## 接续开发前检查

```powershell
uv run ruff check src tests scripts
uv run pytest
uv run work-harness check-compatibility
uv run work-harness eval
cd frontend
npm run type-check
npm run test -- --run
npm run build
```

当前基线为后端 56 项测试、前端 13 项测试和 10/10 Mock 契约评估通过。Mock 评估只验证工程流程，不代表真实模型质量。
