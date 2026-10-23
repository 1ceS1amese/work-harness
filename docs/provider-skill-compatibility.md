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

## Responses、Chat Completions 与 Claude Messages

`openai_compatible` 是协议 Adapter，不代表 OpenAI 官方 Provider，也不绑定官方域名或模型名。任何自定义模型网关都可以通过 `protocol_variant`、`api_family`、自定义 Base URL、环境变量名、`allowed_models` 和映射完成组装：

| `api_family` | 请求路径 | 结构化输出 | Tool 定义与调用 |
|---|---|---|---|
| `chat_completions` | `/v1/chat/completions` | OpenAI 协议默认 `json_schema`；DeepSeek 方言默认 `json_object` 并追加 JSON Schema 指令 | Chat `tools[].function` 与 `message.tool_calls` |
| `responses` | `/v1/responses` | `text.format`，默认 `json_schema` | 扁平函数 Tool、`function_call`、`function_call_output` |
| `messages`（`anthropic` Adapter） | `/v1/messages` | 默认使用 JSON Schema 提示词并解析 JSON；显式 `json_schema` 使用 `output_config.format` | `input_schema`、`tool_use`、`tool_result` |

Chat 模式下 OpenAI 协议默认发送 `max_completion_tokens`，DeepSeek 方言默认发送 `max_tokens`；两者均可由 Profile 的显式字段覆盖。Responses 模式发送 `max_output_tokens`，并默认 `store: false`。Adapter 会把 Responses 函数调用还原为框架内部统一的 Chat Tool Call 形状，并在下一轮保留 typed output items；DeepSeek Chat 也会保留 Tool Call 所需的 `reasoning_content`，因此 Workflow 和 ToolGateway 不感知协议差异。

`base_url` 可填域名、IP、容器服务名或完整接口地址。显式 HTTP / HTTPS 原样采用；未写协议时，公网域名默认 HTTPS，本机、私有 IP、IPv6 回环、单标签容器名与 `.local` / `.internal` 默认 HTTP，80 / 443 端口分别对应 HTTP / HTTPS。不会因 TLS 或网络错误自动降级。根地址自动补 `/v1`；完整的 `/responses`、`/chat/completions`、`/messages` 或 `/models` 地址先还原基础路径，再拼接调用路径。代理前缀与非敏感查询参数会保留。页面粘贴前三种完整推理端点时同步选择相应 API 协议；根地址无法表达请求格式，沿用当前选择，模型列表不用于猜测协议支持能力。

页面不再提供网络许可勾选框或请求地址预览。`allow_insecure_http` / `allow_private_network` 默认及页面保存值为 `null`（根据地址自动兼容），仍接受旧配置的显式布尔限制；部署显式 `false` 仍会禁止相应连接。`base_url_env` 有值时优先于 `base_url`，并经过同一个地址归一化与校验过程。凭据不得放进地址或查询字段；模型发现继续禁止跟随重定向。具体模型方是否开放 Responses、结构化输出和 Tool Call 仍需真实验收。

Claude 默认发送 `x-api-key` 与 `anthropic-version: 2023-06-01`，OpenAI 默认发送 `Authorization: Bearer`。`auth_scheme` 可显式选择 `bearer` / `x_api_key` 以兼容网关，`auto` 跟随协议。Claude 的系统指令、工具声明、工具结果与签名 thinking 块独立适配；跨协议时使用可移植的工具调用历史。Claude 输入 Token 总数是普通输入、缓存读取和缓存创建之和，缓存读取单独计数；缺失统计标记为不完整，不做费用或额度判定。

## 上游模型发现

`POST /v1/providers/models/discover` 接收 `{config, provider_name?, api_key?, clear_api_key?}`，返回 `{items: [{id, name}], truncated}`。尚未选择模型时传 `config.enabled: false`。后端使用同一地址与鉴权请求上游 `/models`，支持 OpenAI 的 `data[].id` 和 Claude 的 `data[].display_name`、`has_more` / `last_id` 分页。编辑已有连接时可以省略 Key，由后端从加密存储读取；环境变量密钥同样适用。此接口不写入设置，不创建任务。

界面提供搜索、多选、默认模型及按阶段 / Agent 分配，并保留手动 ID 输入。切换连接参数会清除发现结果并取消旧请求；拉取失败或空列表不会删除已选模型。上游发现不等于推理能力验证，不能据模型名断言 Responses、工具或严格 JSON Schema 支持。

读取最多 20 页 / 5000 个模型，每页最多 2 MiB，总时限最多 30 秒；不跟随上游重定向或分页 URL。列表不存在、鉴权失败、非 JSON、无效分页均返回可重试或手填提示。部署鉴权与上游鉴权分开处理，Provider 校验错误不回显包含密钥的请求输入。

参照推理端点而非网关管理 API：[OpenAI Models](https://developers.openai.com/api/reference/resources/models/methods/list)、[Responses 与 Chat](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Claude Models](https://platform.claude.com/docs/en/api/models/list)、[Claude 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)、[new-api 转发路由](https://github.com/QuantumNous/new-api/blob/main/router/relay-router.go)、[sub2api 网关路由](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/server/routes/gateway.go)。本项目未复制网关源码。

## Provider Profile 持久化

`GET /v1/providers` 同时展示 YAML 默认值与 SQLite 覆盖，并只报告端点/凭据是否可用，不返回
环境变量值或页面保存的 Key。`PUT /v1/providers/{name}` 保存 Profile 以及可选的新 Key，
`DELETE` 删除覆盖和对应密文。模型映射示例：

工作台“设置 → 模型连接”打开独立的 `/settings?section=models` 页面。界面支持先以
`enabled: false` 暂存，并在环境变量注入与页面加密保存之间选择；保存后会显示待重启、端点和
凭据就绪状态。

```json
{
  "config": {
    "adapter": "openai_compatible",
    "protocol_variant": "openai",
    "api_family": "responses",
    "enabled": false,
    "base_url": "https://team-gateway.example/v1",
    "credential_source": "environment",
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

`main` 是主协调组别名，会展开为所有非 assignable 节点；每个 `worker-*` 子 Agent 可单独选择
同一或不同 Provider Profile。多个启用 Profile 不能同时绑定同一个 Agent。写入前先按完整配置
进行 fail-closed 校验；写入后重启装载，不在运行中关闭旧 HTTP Client。

凭据合同：

- `credential_source: environment` 要求 `api_key_env`，实际 Key 只存在于进程环境；
- `credential_source: stored` 禁止同时设置 `api_key_env`，Key 通过 PUT 请求的顶层
  `api_key` 一次性提交，加密后保存；
- PUT 不带 `api_key` 时保留旧密文，`clear_api_key: true` 显式清除；切回环境变量通道也会清除
  不再使用的旧密文；
- Profile 响应只包含 `credential_configured`，不回显 Key、密文或加密密钥。

Provider Adapter 通过注册表按配置名称组装。内置 Adapter 是 `mock`、`openai_compatible` 和 `anthropic`；外部包可声明入口点：

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

## 任务模型与推理档位

聊天框右下角可选择已验证且已加载的模型；「跟随项目设置」继续使用既有逻辑模型及 Agent 分配。推理档位主要在模型连接设置页调整，每个模型分别保存默认值。选择具体模型后，任务中的 `model_selection: {provider, model, reasoning_effort?}` 固定整次运行的路由；省略档位时，在创建任务时取设置页的默认值并持久化。显式选择不会静默回退到其他模型，仍须满足 Skill 的 Provider 限制。

- `POST /v1/providers/models/verify`：在模型发现请求的基础上增加 `model`、`efforts`，以及可选 `confirmed_efforts`。先验证默认调用，再执行无效参数对照和各档位的调用；使用固定 JSON 小任务与工具定义，不执行任何工具。单请求 20 秒，档位探测并发 2，总请求含排队不超过 180 秒，不自动重试。
- `GET /v1/models`：返回当前进程已加载的可选模型、默认档位及已支持档位；同一目录也包含在 `/v1/capabilities.model_choices`。
- `PUT /v1/providers/{name}`：沿用原保存接口，在 `config.model_settings[model]` 中保存服务端返回的 `verification` 和 `default_effort`。新验证记录须在一小时内保存；已经保存的记录重启后继续读取。服务端拒绝伪造记录及未经验证的默认档位。

Responses 使用 `reasoning.effort`，Chat 使用 `reasoning_effort`，Claude 使用 `output_config.effort` 和 `thinking.type=adaptive`，与原有 JSON Schema 合并。旧 Claude 模型若不接受自适应思考报文，继续保留默认调用；不将字符串档位臆测成 Token 预算。`none/minimal/low/medium/high/xhigh/max` 逐项测试；额外名称可在更多验证选项中添加，不做档位别名映射。

验证结果区分参数校验依据和渠道使用者确认依据。HTTP 成功但上游接受任意无效档位时，默认标为未确认；使用者可填写渠道明确确认的档位，仍须实际调用成功才开放，并显示「渠道已确认」。鉴权、限流、超时和不完整响应不会标成不支持或通过。记录绑定当前地址、协议、密钥及报文配置，连接变更后需重新测试；验证只证明当时的请求兼容性，不证明推理质量或渠道未来行为。
