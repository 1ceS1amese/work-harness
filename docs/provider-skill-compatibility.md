# Provider、Tool 与 Skill 兼容性设计

## 目标

Work Harness 把任务能力拆成可组合的 Skill，并让每个启用的 Provider 遵守同一运行协议和能力集合。用户可以选择一个或多个 Skill，再让框架自动选择 Provider，或在该 Skill 的兼容范围内手动固定 Provider。

运行时采用 fail-closed 策略：Provider、Tool 或 Skill 的声明不一致时拒绝启动；任务选择了不可用组合时拒绝创建运行，不在执行中静默降级。

## Provider 合同

每个 Provider 必须实现 `ModelProvider`：

- `name` 与配置键一致；
- `protocol_version` 与 `models.compatibility.protocol_version` 一致；
- `capabilities` 与配置声明完全一致；
- 所有启用 Provider 的能力集合完全相同，并覆盖基线能力；
- 实现异步 `complete()` 和 `healthcheck()`。

当前可移植能力集合为 `chat`、`structured_output`、`tool_calling`。兼容性报告在启动时生成，并由 `/health`、`/v1/capabilities` 和 `/v1/compatibility` 暴露。

## OpenAI 协议、DeepSeek 方言与双端点适配

`openai_compatible` 是协议 Adapter，不代表 OpenAI 官方 Provider，也不绑定官方域名或模型名。任何自定义模型网关都可以通过 `protocol_variant`、`api_family`、自定义 Base URL、环境变量名、`allowed_models` 和映射完成组装：

| `api_family` | 请求路径 | 结构化输出 | Tool 定义与调用 |
|---|---|---|---|
| `chat_completions` | `/v1/chat/completions` | OpenAI 协议默认 `json_schema`；DeepSeek 方言默认 `json_object` 并追加 JSON Schema 指令 | Chat `tools[].function` 与 `message.tool_calls` |
| `responses` | `/v1/responses` | `text.format`，默认 `json_schema` | 扁平函数 Tool、`function_call`、`function_call_output` |

Chat 模式下 OpenAI 协议默认发送 `max_completion_tokens`，DeepSeek 方言默认发送 `max_tokens`；两者均可由 Profile 的显式字段覆盖。Responses 模式发送 `max_output_tokens`，并默认 `store: false`。Adapter 会把 Responses 函数调用还原为框架内部统一的 Chat Tool Call 形状，并在下一轮保留 typed output items；DeepSeek Chat 也会保留 Tool Call 所需的 `reasoning_content`，因此 Workflow 和 ToolGateway 不感知协议差异。

`base_url` 应指向自定义模型方的 API 根路径（通常包含 `/v1`），Adapter 再追加端点路径。`base_url_env` 有值时优先于持久化的 `base_url`，便于不同部署覆盖。具体模型方是否开放 Responses、结构化输出和 Tool Call 仍需真实验收；配置声称支持但运行 Adapter 不满足统一能力时会 fail-closed。

## Provider Profile 持久化

`GET /v1/providers` 同时展示 YAML 默认值与 SQLite 覆盖，并只报告端点/凭据是否可用，不返回环境变量值。`PUT /v1/providers/{name}` 保存 Profile，`DELETE` 删除覆盖。模型映射示例：

前端群聊顶部的齿轮按钮会打开同一套 API 的可视化管理抽屉。界面只编辑 Provider 的非敏感 Profile，支持先以 `enabled: false` 暂存；保存后会显示待重启、端点就绪和密钥环境变量就绪状态。

```json
{
  "config": {
    "adapter": "openai_compatible",
    "protocol_variant": "openai",
    "api_family": "responses",
    "enabled": false,
    "base_url": "https://team-gateway.example/v1",
    "api_key_env": "TEAM_GATEWAY_API_KEY",
    "allowed_models": ["main-model", "worker-a", "worker-b"],
    "capabilities": ["chat", "structured_output", "tool_calling"]
  },
  "model_bindings": {
    "planner-strong": "main-model",
    "worker-fast": "worker-a"
  },
  "agent_bindings": {
    "main": "main-model",
    "worker-01": "worker-a",
    "worker-02": "worker-b"
  }
}
```

`main` 是主协调组别名，会展开为所有非 assignable 节点；每个 `worker-*` 子 Agent 可单独选择同一或不同 Provider Profile。多个启用 Profile 不能同时绑定同一个 Agent。写入前先按完整配置进行 fail-closed 校验；写入后重启装载，不在运行中关闭旧 HTTP Client。API Key 只能通过 `api_key_env` 指向的环境变量注入。

Provider Adapter 通过注册表按配置名称组装。内置 Adapter 是 `mock` 和 `openai_compatible`；外部包可声明入口点：

```toml
[project.entry-points."work_harness.providers"]
custom_model = "my_package.provider:build_provider"
```

Builder 签名为 `(provider_name, ProviderConfig) -> ModelProvider`。新增 Adapter 后仍必须通过相同的兼容性检查。

## Tool 合同

Tool 由 `ToolAdapterRegistry` 按 `config/tools.yaml` 的 Tool 名称组装。外部 Tool 使用 `work_harness.tools` 入口点，Builder 签名为 `(project_root: Path) -> ToolHandler`：

```toml
[project.entry-points."work_harness.tools"]
knowledge_read = "my_package.tools:build_knowledge_read"
```

Handler 的 `name` 必须与配置键一致，并提供输入/输出 Pydantic 模型和异步 `execute()`。原有角色白名单、风险级别、审批、超时和结果截断规则继续由 Tool Gateway 强制执行。

## Skill 包

可运行 Skill 位于 `skills/<skill-id>/`：

```text
skills/my-skill/
├── SKILL.md
└── agents/
    ├── openai.yaml
    └── work-harness.yaml
```

`SKILL.md` 使用标准 `name`、`description` frontmatter，正文是注入 Worker system message 的任务指令。`agents/work-harness.yaml` 是机器可读路由合同：

```yaml
schema_version: 1
version: 1.0.0
logical_model: worker-fast
required_capabilities: [chat, structured_output, tool_calling]
allowed_tools: [workspace_read]
compatible_providers: ["*"]
preferred_providers: [mock-secondary, mock, compatible]
applies_to: [risk]
```

`["*"]` 表示接受任何通过 `required_capabilities` 和逻辑候选校验的自定义 Provider；如果 Skill 只能在已专项验收的少数模型方运行，也可以显式列名。启动时会检查逻辑模型、Provider、能力、Tool 及所有可指派角色的 Tool 权限。多个 Skill 分配到同一子任务时，必须使用同一个逻辑模型，并至少有一个共同的可用 Provider；Tool 和能力取并集。手动 Provider 覆盖只有在所有组合 Skill 的共同可用集合内才会生效。

`skills/create-provider-skill` 是仓库内的编写指南；`repository-analysis` 和 `risk-audit` 是可直接选择的示例。

## 使用

前端输入框上方的“技能”菜单可选择 Skill 和 Provider。API 请求示例：

```json
{
  "goal": "审计这个仓库的交付风险",
  "skills": [
    {"skill_id": "risk-audit", "provider": "mock-secondary"}
  ]
}
```

CLI 支持同样的选择：

```bash
work-harness skills list
work-harness skills validate
work-harness check-compatibility
work-harness run --goal "审计交付风险" --skill risk-audit@mock-secondary
```

`provider` 可以省略，此时按照 `preferred_providers` 排序并保留有界 fallback。模型调用记录会保存实际 Provider 和 `skill_ids`，便于复核路由结果。

## 新增 Skill 的验收清单

1. 使用 `skills/create-provider-skill` 的标准结构与清单。
2. 在 `work-harness.yaml` 中只声明真实需要的能力和 Tool。
3. 将 Provider 加入逻辑模型候选，并保证所有启用 Provider 的能力集合相同。
4. 执行 `work-harness skills validate`。
5. 执行后端测试、前端类型检查与构建。
6. 对至少一个自动路由和一个手动 Provider 路由增加测试。
