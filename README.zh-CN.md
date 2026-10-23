<a id="readme-top"></a>

<div align="center">
  <h1>Work Harness</h1>
  <p>基于 LangGraph，让多个 Agent 围绕共享黑板协作。</p>
  <p>
    <a href="docs/blackboard-design.md"><strong>阅读架构设计 »</strong></a>
    <br /><br />
    <a href="#getting-started">快速开始</a>
    &middot;
    <a href="https://github.com/1ceS1amese/work-harness/issues/new?template=bug.yml">报告问题</a>
    &middot;
    <a href="https://github.com/1ceS1amese/work-harness/issues/new?template=feature.yml">提出功能建议</a>
  </p>
</div>

[![CI][ci-shield]][ci-url]
[![Python][python-shield]][python-url]
[![License: MIT][license-shield]][license-url]
[![Status: Alpha][status-shield]][status-url]

[English](README.md) | **简体中文**

<details>
  <summary>目录</summary>
  <ol>
    <li><a href="#about-the-project">项目介绍</a>
      <ul><li><a href="#built-with">技术栈</a></li></ul>
    </li>
    <li><a href="#getting-started">快速开始</a>
      <ul>
        <li><a href="#prerequisites">环境要求</a></li>
        <li><a href="#installation">源码安装</a></li>
        <li><a href="#other-deployment-options">其他部署方式</a></li>
      </ul>
    </li>
    <li><a href="#usage">使用方式</a>
      <ul>
        <li><a href="#web-workbench">网页工作台</a></li>
        <li><a href="#command-line">命令行</a></li>
        <li><a href="#http-api">HTTP API</a></li>
        <li><a href="#token-accounting">Token 记录</a></li>
      </ul>
    </li>
    <li><a href="#roadmap">项目进度与后续方向</a></li>
    <li><a href="#contributing">参与贡献</a></li>
    <li><a href="#license">许可证</a></li>
    <li><a href="#contact">联系与反馈</a></li>
    <li><a href="#acknowledgments">致谢</a></li>
  </ol>
</details>

<a id="about-the-project"></a>

## 项目介绍

Work Harness 是一个用于运行、查看和恢复多 Agent 任务的框架与工作台。每次运行拥有一块共享
黑板和一个根目标。多个知识源读取已经提交的状态，提出新的信息；LangGraph 控制器根据目标
缺口选择下一批可执行的知识源。系统如何向前推进，取决于黑板上已经留下了什么，无需预先
编排完整的任务流程。

黑板上只有三种知识对象：

| 对象 | 含义 | 协作中的作用 |
| --- | --- | --- |
| **Fact** | 在明确范围内已确认的主张 | 为后续处理和目标验收提供依据 |
| **Intent** | 尚待探索的目标或方向 | 记录验收条件与未解决的问题 |
| **Hint** | 经验提示、假设或建议 | 引导探索，不直接作为已确认的事实 |

知识源可以采用规则、观察、验证逻辑或独立的 LangGraph Agent 子图。它们通过有版本的板面对象
交换知识，私有模型对话保留在各自子图中。黑板内核在接受变更前检查证据、来源、冲突和对象
有效性。

当前已具备：

- **状态驱动协作：** 有界并行激活、按 Intent 分配并发名额、重试、超时、人工输入和明确验收。
- **可查看的执行过程：** 目标缺口、对象历史、证据、附件、知识关系图、实时运行更新和持久事件查询。
- **故障恢复：** 单应用进程使用 SQLite；API/Worker 分离时使用 PostgreSQL、持久队列和执行租约，
  支持取消、故障接管与恢复。
- **模型与能力配置：** HTTP / HTTPS 上游、Chat Completions / Responses / Claude Messages、上游模型发现与选择、DeepSeek 协议方言、
  独立 Agent 路由、提示词、Tool 和 Skill。
- **旧引擎兼容：** 通过 `engine=legacy` 显式使用原有的规划、执行、复核和汇总流程。

新任务默认使用 **`blackboard`**。内置知识源生成分析报告、实施方案和操作清单，并验证成果存储与章节结构；若要
确认领域事实或业务操作，需要接入相应的 Tool 和验证器。模型自报置信度不能直接形成 Fact。

<a id="built-with"></a>

### 技术栈

| 层次 | 技术 |
| --- | --- |
| Agent 执行 | Python、LangGraph |
| API 与数据契约 | FastAPI、Pydantic |
| 网页工作台 | Vue 3、Nuxt UI、官方 Vue Dashboard 模板、TypeScript、Vite |
| 持久化 | SQLite、PostgreSQL、LangGraph Checkpoint |
| 桌面打包 | pywebview、PyInstaller、Windows WebView2 |
| 验证工具 | pytest、Vitest、Ruff、GitHub Actions |

实现细节见[黑板设计](docs/blackboard-design.md)、[持久化契约](docs/persistence.md)和
[旧版多 Agent 设计](docs/multi-agent-design.md)。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

<a id="getting-started"></a>

## 快速开始

<a id="prerequisites"></a>

### 环境要求

运行当前源码需要 Python **3.11+**、[uv](https://docs.astral.sh/uv/)、Node.js **22+**、npm 和 Git。
使用真实模型还需要兼容服务的接口地址、凭据和模型名称，模型应支持聊天、结构化输出和工具调用。

默认 `setup` Profile 会在真实模型配置完成前保持配置模式。确定性的 Mock Provider 仅用于
显式选择的 `local` 开发 Profile，供工程检查使用。

<a id="installation"></a>

### 源码安装

克隆项目并安装锁定版本的依赖：

```bash
git clone https://github.com/1ceS1amese/work-harness.git
cd work-harness
uv sync --locked --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run work-harness check-config
uv run work-harness serve --host 127.0.0.1 --port 8000
```

打开[本地工作台](http://127.0.0.1:8000/)，然后：

1. 进入“设置 → 模型连接”，选择 Responses、Chat Completions 或 Claude Messages，填写上游地址与 API Key。
   可连接官方服务或 new-api / sub2api 网关；使用 HTTP 或本机地址时打开相应连接开关。
2. 点击“获取模型”，搜索并勾选模型，再设置默认模型或单独分配给 Agent。也可手动填写模型 ID，启用连接后保存。
3. 重启服务，使暂存配置生效。模型配置可用前，就绪检查和任务提交保持不可用。
4. 创建会话、填写目标，并明确选择完成条件。

内嵌模式的设置和运行数据默认保存在 `var/` 下。页面提交的模型凭据在 Provider 存储中加密保存，
恢复时需要配套的独立密钥文件。[环境变量示例](.env.example)列出了环境配置项。源码 CLI 不会
自动加载 `.env`；使用环境配置时，需要将变量导出到启动进程。

<a id="other-deployment-options"></a>

### 其他部署方式

**Docker Compose：单应用进程。**

```bash
./scripts/compose.sh up
# 在工作台配置模型后，加载保存的设置。
./scripts/compose.sh reload
```

**PostgreSQL：独立 API 与 Worker。**

```bash
./scripts/compose.sh init-distributed
# 启动前填写 .env.distributed 和 deploy/runtime.distributed.env。
./scripts/compose.sh distributed
./scripts/compose.sh scale-workers 3
```

分布式实例必须使用一致的配置，该模式暂不允许通过设置页修改 Provider、提示词和运行参数。
SQLite 仅支持一个应用进程；多个执行 Worker 使用 PostgreSQL。环境要求、HTTPS、备份恢复和
回退方法见[部署指南](docs/docker-deployment.md)。

**Windows 桌面版与 Python wheel：** 仓库提供打包工作流及
[发布产物入口](https://github.com/1ceS1amese/work-harness/releases)。桌面版面向 Windows 10/11 x64，
需要 WebView2；wheel 包含后端和内置配置，不含工作台前端。请按[Windows 指南](docs/windows-executable.md)
和所选版本的发布说明使用。当前源码中的变更不代表已有 EXE 或镜像已包含这些变更。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

<a id="usage"></a>

## 使用方式

<a id="web-workbench"></a>

### 网页工作台

选择目标、完成条件及需要的 Skill 或 Agent 分工。打开任务详情，可以查看尚未满足的目标、
事实、探索方向、提示及其关系。人工补充会绑定对应的 Intent 和板面版本。

完成条件按交付物选择：**分析报告**（结论、依据、风险与建议）、**实施方案**（范围、步骤、验收与回退）、
**操作清单**（前提、待办项与检查方法）。选择后会将相应要求传给 Agent；后端核对保存内容及必要章节，
操作清单还须包含待办项。结构检查不代表内容事实已核实或文档中的操作已经执行。
旧的 `saved-draft` 合同仍可通过 CLI / API 使用，保留原有的仅存储验证行为。

设置入口为 `/settings?section=models`、`/settings?section=prompts` 和 `/settings?section=runtime`。
部分提示词和复核选项属于旧引擎；黑板中的 Agent 保留各自的私有执行逻辑。

<a id="command-line"></a>

### 命令行

完成模型配置后：

```bash
# 默认使用黑板；选择需要交付的文档类型。
uv run work-harness run \
  --goal "为团队设计项目上线方案" --acceptance-template implementation-plan

# 显式选择旧版规划与复核流程。
uv run work-harness run --engine legacy --goal "审查这份交付计划"

# 检查配置与扩展契约。
uv run work-harness check-compatibility
uv run work-harness skills list
uv run work-harness skills validate
```

自定义验收使用 `--blackboard-spec goal.json`，文件须包含 `topic` 和已注册知识源能够处理的
类型化 `acceptance` 条件。参见[任务契约](src/work_harness/domain/models.py)与
[黑板设计](docs/blackboard-design.md)。CLI 执行任务使用内嵌模式；分布式任务通过 API 提交。

如需离线验证执行契约，可在 POSIX shell 中显式使用确定性的测试模型：

```bash
WORK_HARNESS_PROFILE=local uv run work-harness run \
  --goal "验证黑板执行契约" --acceptance-template saved-draft
```

Mock 结果只能验证运行契约，不能证明真实模型质量。建议使用独立的开发检出目录，避免与既有
Provider 覆盖配置和应用数据混用。

<a id="http-api"></a>

### HTTP API

提交带明确验收条件的任务：

```bash
curl --request POST http://127.0.0.1:8000/v1/runs \
  --header 'Content-Type: application/json' \
  --data '{
    "goal": "起草项目介绍",
    "blackboard": {
      "topic": "document",
      "acceptance": [{
        "id": "saved-draft",
        "description": "保存可读取的候选草稿，不验证内容准确性",
        "claim_key": "draft.content",
        "claim_type": "delivery",
        "scope": {"verification": "stored_content_only"},
        "required_checks": ["content_stored"]
      }]
    }
  }'
```

响应包含 `run_id`。示例使用默认本地配置；启用鉴权的部署还需要携带部署 API Key。

| 方法 | 接口 | 用途 |
| --- | --- | --- |
| `GET` | `/health/live` · `/health/ready` | 进程存活与部署就绪状态 |
| `GET` | `/v1/capabilities` | Agent、Tool、Skill、默认引擎及验收模板 |
| `POST` | `/v1/runs` | 提交任务，支持幂等键 |
| `GET` | `/v1/runs/{run_id}` | 状态、输出与已记录用量 |
| `GET` | `/v1/runs/{run_id}/events` | SSE 运行更新 |
| `GET` | `/v1/runs/{run_id}/blackboard` | 板面概况与目标缺口 |
| `GET` | `/v1/runs/{run_id}/blackboard/objects` | 查询指定板面版本的对象 |
| `GET` | `/v1/runs/{run_id}/blackboard/events` | 查询持久化黑板事件 |
| `POST` | `/v1/runs/{run_id}/inputs` | 向黑板 Intent 提交带版本的人工输入 |
| `DELETE` | `/v1/runs/{run_id}` | 取消任务 |

所选 Profile 启用 API 文档时，可在 `/docs` 查看交互式接口说明。

<a id="token-accounting"></a>

### Token 记录

输入、输出和缓存命中 Token 分别记录。缓存 Token 已包含在输入中，总量只计算 **输入 + 输出**。
缺失或不完整的用量明确标记为未确认；重复回执和恢复过程不会重复累计同一次调用。

系统不使用累计 Token 或金额额度拦截任务。`max_output_tokens` 只控制单次模型响应的输出长度，
执行仍受超时、步数、激活次数、重试和并发限制约束。配置见 [Agent 配置](config/agents.yaml)与
[运行配置](config/runtime.yaml)。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

<a id="roadmap"></a>

## 项目进度与后续方向

**当前源码进度：2026-10-22。** 黑板核心、有界并行控制器、网页/API/CLI 入口、知识关系图、
人工输入、SQLite/PostgreSQL 恢复、备份回退路径及默认引擎切换均已实现。Token 记账已与花销额度
控制分离。详细实施记录统一保留在[现有设计计划](docs/blackboard-design.md)中。

后端全量回归与专项复测共 **927 个不同用例验证通过**，覆盖 SQLite、真实 PostgreSQL 16 API/Worker、记账、失败、恢复和兼容性。已优化设置读取、运行历史索引和超时/取消处理，测量范围见 [后端性能记录](docs/backend-performance.md)；前端 **57 项组件/状态测试及 38 项桌面/手机浏览器回归通过**，类型检查与生产构建通过。工作台采用参考 Codex 风格的统一布局：深浅色任务主页、可搜索会话导航、共用单侧栏的独立设置页、统一折叠的任务设置、聊天框右下角模型选择、设置页逐模型推理验证和按需打开的黑板详情，见 [GUI 设计与验收](docs/gui-design.md)。此前使用 `gpt-5.6-sol`
完成的六任务对照中，黑板通过 **6/6**，legacy 通过 **1/6**。这只是有限任务集的结果，不代表
通用任务质量；该对照早于最新 Token 重构。日期、测试批次和评估范围见[开发记录](docs/development-status.md)。

后续待完善的内容包括：

- 按当前源码重新构建并验证 Windows 发布产物，补做容器恢复验证。
- 扩大真实任务评估，接入业务 Tool、领域验证器及跨 Run 经验复用。
- 服务端会话同步、用户身份与租户隔离。
- 分布式环境共享且可编辑的 Provider 和应用设置存储。

历史任务保留自己的引擎和执行身份，继续执行时需要匹配原执行版本与配置。停止接收新黑板
任务时，将 `WORK_HARNESS_WORKFLOW_ENGINE=legacy`、`WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS=false`
设置到部署环境，并一致重启各应用实例；这不会迁移旧检查点。

部署 API Key 是共享的服务凭据，不提供用户或租户边界。扩展部署前请阅读 [SECURITY.md](SECURITY.md)
与[分布式队列契约](docs/distributed-run-queue.md)。功能建议和已知问题通过
[GitHub Issues](https://github.com/1ceS1amese/work-harness/issues)跟踪。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

<a id="contributing"></a>

## 参与贡献

请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。较大的
行为或架构调整先通过 Issue 讨论，随后创建聚焦的分支或 Fork，提交包含行为说明与验证结果的
Pull Request。公开接口变化时，请同步维护中英文 README。

常用开发检查：

```bash
uv run ruff check src tests scripts
uv run pytest
npm --prefix frontend run type-check
npm --prefix frontend test
npm --prefix frontend run build
```

构建后可运行浏览器回归（使用隔离的 API 响应样例，不调用真实模型）：

```bash
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend run test:e2e
```

PostgreSQL 测试需要将 `WORK_HARNESS_TEST_DATABASE_URL` 指向专用测试数据库，未配置时会跳过。
数据库、打包与部署检查见[贡献指南](CONTRIBUTING.md)。安全漏洞请按 [SECURITY.md](SECURITY.md)
中的私密渠道报告。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

<a id="license"></a>

## 许可证

本项目采用 [MIT License](LICENSE)。Copyright © 2026 Work Harness contributors。

<a id="contact"></a>

## 联系与反馈

- 项目仓库：[1ceS1amese/work-harness](https://github.com/1ceS1amese/work-harness)
- 问题与建议：[Issue 模板](https://github.com/1ceS1amese/work-harness/issues/new/choose)
- 变更与发布：[CHANGELOG.md](CHANGELOG.md) · [GitHub Releases](https://github.com/1ceS1amese/work-harness/releases)

<a id="acknowledgments"></a>

## 致谢

- [Best-README-Template](https://github.com/othneildrew/Best-README-Template)：README 组织结构。
- [LangGraph](https://github.com/langchain-ai/langgraph)、[FastAPI](https://fastapi.tiangolo.com/) 和
  [Pydantic](https://docs.pydantic.dev/)：执行与 API 基础。
- [官方 Vue Dashboard 模板](https://github.com/nuxt-ui-templates/dashboard-vue)（[MIT 许可](docs/licenses/nuxt-ui-dashboard.txt)）。
- [Vue](https://vuejs.org/)、[Nuxt UI](https://ui.nuxt.com/)、
  [pywebview](https://pywebview.flowrl.com/) 和 [PyInstaller](https://pyinstaller.org/)：工作台与桌面工具链。

<p align="right">(<a href="#readme-top">返回顶部</a>)</p>

[ci-shield]: https://github.com/1ceS1amese/work-harness/actions/workflows/ci.yml/badge.svg
[ci-url]: https://github.com/1ceS1amese/work-harness/actions/workflows/ci.yml
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white
[python-url]: pyproject.toml
[license-shield]: https://img.shields.io/badge/License-MIT-blue?style=flat-square
[license-url]: LICENSE
[status-shield]: https://img.shields.io/badge/Status-Alpha-orange?style=flat-square
[status-url]: docs/development-status.md
