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

Provider Adapter 通过注册表按配置名称组装。内置 Adapter 是 `mock` 和 `openai_compatible`；外部包可声明入口点：

```toml
[project.entry-points."work_harness.providers"]
vendor = "my_package.provider:build_provider"
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
compatible_providers: [mock, mock-secondary, compatible]
preferred_providers: [mock-secondary, mock, compatible]
applies_to: [risk]
```

启动时会检查逻辑模型、Provider、能力、Tool 及所有可指派角色的 Tool 权限。多个 Skill 分配到同一子任务时，必须使用同一个逻辑模型，并至少有一个共同的可用 Provider；Tool 和能力取并集。手动 Provider 覆盖只有在所有组合 Skill 的共同可用集合内才会生效。

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
