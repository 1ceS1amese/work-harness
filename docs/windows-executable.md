# Windows 单文件版

Work Harness 从 `v0.2.0` 起提供 Windows x64 单文件 `work-harness.exe`。它包含 Python
运行时、FastAPI 后端、Vue 工作台、内置配置、Prompt、Schema、评估用例和 Skill，不要求目标
电脑预装 Python、Node.js 或 npm。

## 下载与启动

1. 从 [GitHub Releases](https://github.com/1ceS1amese/work-harness/releases/latest) 下载
   `work-harness.exe` 和 `work-harness.exe.sha256`。
2. 在 PowerShell 中校验：

   ```powershell
   (Get-FileHash .\work-harness.exe -Algorithm SHA256).Hash.ToLowerInvariant()
   Get-Content .\work-harness.exe.sha256
   ```

3. 双击 `work-harness.exe`。程序会启动本地服务并打开
   `http://127.0.0.1:8000/`；关闭控制台或按 `Ctrl+C` 即停止服务。

程序仅监听本机回环地址。第二次双击不会创建第二个 Windows 服务进程，而是打开已运行的工作台。
首次启动时 PyInstaller 会进行临时解包，因此可能比后续页面刷新慢几秒。

## 持久化目录

内置文件是只读发布资源。可写状态默认保存在：

```text
%LOCALAPPDATA%\WorkHarness\
├── data\checkpoints.sqlite
├── data\runs.sqlite
├── data\providers.sqlite
├── reports\latest-evaluation.json
├── exports\schemas\
└── logs\
```

替换或移动 EXE 不会删除这些数据。Provider Profile 只保存 Base URL、环境变量名称、模型白名单
和路由，不保存 API Key 值。删除上述目录会永久删除本机运行历史和 Provider 配置，请先停止程序并
自行备份。

可在启动前通过 `WORK_HARNESS_DATA_ROOT` 改变整个数据根目录，也可使用
`WORK_HARNESS_CHECKPOINT_PATH`、`WORK_HARNESS_RUN_STORE_PATH` 和
`WORK_HARNESS_PROVIDER_STORE_PATH` 分别覆盖数据库位置。

## 自定义模型环境变量

无需在安装阶段配置密钥。先在 Web 界面保存 OpenAI 协议或 DeepSeek 方言的 Provider Profile，
并填写 Base URL 与 API Key 的“环境变量名称”。然后关闭程序，在同一 PowerShell 会话中设置对应
变量并重新启动，例如：

```powershell
$env:MY_MODEL_BASE_URL = "https://model.example.com/v1"
$env:MY_MODEL_API_KEY = Read-Host "API Key"
.\work-harness.exe
```

以上变量名只是示例；应与界面中保存的名称一致。不要把真实密钥提交到仓库或写入配置文件。

## EXE 的命令行模式

EXE 带参数时不会自动启动浏览器，可直接复用原 CLI：

```powershell
.\work-harness.exe check-config
.\work-harness.exe check-compatibility
.\work-harness.exe skills validate
.\work-harness.exe serve --host 127.0.0.1 --port 8765
```

`--project-root PATH` 必须放在子命令之前，可加载外部配置、Prompt、Schema 和 Skill：

```powershell
.\work-harness.exe --project-root C:\work-harness-config check-config
```

## 从源码构建

Windows x64 构建机需要 Python 3.12、uv、Node.js 22 和 npm：

```powershell
.\scripts\build_windows.ps1
```

脚本执行锁定依赖安装、前端生产构建、PyInstaller 单文件构建、冻结配置冒烟测试，并生成
`dist\work-harness.exe` 与 SHA-256 文件。PyInstaller 不支持从 Linux 交叉编译 Windows EXE；
仓库的 `Windows EXE` GitHub Actions 工作流在 `windows-latest` 上执行正式发布构建。

## 当前边界

- 发布文件尚未进行商业代码签名，Windows SmartScreen 可能对新下载文件显示警告；应先核对
  SHA-256 和 GitHub 构建来源证明。
- EXE 仍是本地单实例、SQLite 架构，不适合多用户公网服务或多副本部署。
- Python entry-point 插件不会自动注入已封装的 EXE；自定义 Provider/Tool 插件应通过源码或
  自建 spec 重新打包。内置 OpenAI-compatible、DeepSeek、Mock Provider 和 Skill 均可使用。
- 对外部署应使用 Docker Compose 的 `secure`/`production` Profile，而不是桌面启动模式。
