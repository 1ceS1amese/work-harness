# Windows 单文件版

Work Harness 从 `v0.2.0` 起提供 Windows x64 单文件 `work-harness.exe`。它包含 Python
运行时、FastAPI 后端、Vue 工作台、原生桌面窗口、内置配置、Prompt、Schema、评估用例和
Skill，不要求目标电脑预装 Python、Node.js 或 npm。

目标系统为 Windows 10/11 x64，并需要 Microsoft Edge WebView2 Runtime。Windows 11 和大多数
仍受支持的 Windows 10 设备已随 Edge 安装；缺失时启动器会显示明确错误，而不会弹出命令行。

## 下载与启动

1. 从 [GitHub Releases](https://github.com/1ceS1amese/work-harness/releases/latest) 下载
   `work-harness.exe` 和 `work-harness.exe.sha256`。
2. 在 PowerShell 中校验：

   ```powershell
   (Get-FileHash .\work-harness.exe -Algorithm SHA256).Hash.ToLowerInvariant()
   Get-Content .\work-harness.exe.sha256
   ```

3. 双击 `work-harness.exe`。程序不会打开命令行或外部浏览器，而是在独立的 Work Harness
   桌面窗口中启动工作台；关闭该窗口即停止后台本地服务。

首次打开会进入模型配置页，不会启用演示用 Mock Provider。填写接口地址、密钥和模型名称，保存
后关闭并重新启动 EXE；完成前发送任务会被明确阻止，不会显示模拟的完成结果。

程序仅监听本机回环地址。第二次双击不会创建第二个 Windows 服务进程，而是恢复并聚焦已经
运行的桌面窗口。首次启动时 PyInstaller 会进行临时解包，因此可能慢几秒。

## 持久化目录

内置文件是只读发布资源。可写状态默认保存在：

```text
%LOCALAPPDATA%\WorkHarness\
├── data\checkpoints.sqlite
├── data\runs.sqlite
├── data\blackboard.sqlite
├── data\providers.sqlite
├── data\providers.sqlite.key
├── data\settings.json
├── webview\
├── reports\latest-evaluation.json
├── exports\schemas\
└── logs\desktop.log
```

替换或移动 EXE 不会删除这些数据。Provider 可使用环境变量 Key，也可在页面中保存认证加密后的
Key；查询接口不会回显。Agent Prompt 和运行配置保存在 `settings.json`。使用页面凭据时，
`providers.sqlite` 与 `providers.sqlite.key` 必须一起备份和恢复。删除上述目录会永久删除本机
运行历史、Provider 和页面设置，请先停止程序并自行备份。`webview` 目录用于保留桌面窗口的
本地会话与主题设置；模型密钥仍只保存在加密 Provider 数据库中。

可在启动前通过 `WORK_HARNESS_DATA_ROOT` 改变整个数据根目录，也可使用
`WORK_HARNESS_CHECKPOINT_PATH`、`WORK_HARNESS_RUN_STORE_PATH`、`WORK_HARNESS_BLACKBOARD_PATH` 和
`WORK_HARNESS_PROVIDER_STORE_PATH` 分别覆盖数据库位置。当前源码将黑板数据库与其他持久化
数据一同保存在用户目录，已有显式路径覆盖保持优先。若此前曾在资源目录运行黑板任务，
先用原版本和原路径完成或备份这些任务；不自动移动活动黑板或改写其执行指纹。
本轮验证了桌面命令行入口的数据路径，尚未在 Windows 上重新构建和运行 EXE。

## 自定义模型凭据

无需在安装阶段配置密钥。先在 Web 界面保存 OpenAI 协议或 DeepSeek 方言的 Provider Profile，
然后选择以下任一方式：

- “在页面加密保存”：直接输入 API Key，保存并重启 EXE；
- “从环境变量读取”：填写 Base URL 与 API Key 的变量名称，关闭程序后在同一 PowerShell
  会话中设置对应变量并重新启动，例如：

```powershell
$env:MY_MODEL_BASE_URL = "https://model.example.com/v1"
$env:MY_MODEL_API_KEY = Read-Host "API Key"
.\work-harness.exe
```

以上变量名只是示例；应与界面中保存的名称一致。不要把真实密钥提交到仓库、YAML 或日志。

## 诊断和 CLI

桌面 EXE 使用 Windows GUI 子系统，因此不会附着 PowerShell 或显示标准输出。需要
`check-config`、`check-compatibility`、Skill 校验或自定义 `serve` 参数时，请使用源码环境中的
`uv run work-harness ...`；桌面启动失败信息会显示在原生对话框并写入
`%LOCALAPPDATA%\WorkHarness\logs\desktop.log`。

## 从源码构建

Windows x64 构建机需要 Python 3.12、uv、Node.js 22 和 npm：

```powershell
.\scripts\build_windows.ps1
```

脚本执行锁定依赖安装、前端生产构建、PyInstaller 单文件构建、冻结配置冒烟测试，并检查生成
文件的 PE Subsystem 必须为 Windows GUI（无控制台），最后生成 `dist\work-harness.exe` 与
SHA-256 文件。PyInstaller 不支持从 Linux 交叉编译 Windows EXE；仓库的 `Windows EXE`
GitHub Actions 工作流在 `windows-latest` 上执行正式发布构建。

## 当前边界

- 发布文件尚未进行商业代码签名，Windows SmartScreen 可能对新下载文件显示警告；应先核对
  SHA-256 和 GitHub 构建来源证明。
- EXE 仍是本地单实例、SQLite 架构，不适合多用户公网服务或多副本部署。
- Python entry-point 插件不会自动注入已封装的 EXE；自定义 Provider/Tool 插件应通过源码或
  自建 spec 重新打包。内置 OpenAI-compatible、DeepSeek、Mock Provider 和 Skill 均可使用。
- 对外部署应使用 Docker Compose 的 `secure`/`production` Profile，而不是桌面启动模式。
