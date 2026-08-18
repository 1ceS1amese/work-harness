# 项目现状地图与架构诊断

## 扫描基线

初始仓库只有 `agent.md`，共 1095 行；没有源码、入口、依赖文件、测试、配置实现、运行日志、评估脚本或版本库。因此本轮属于从设计文档落地首版工程，不存在可保持兼容的旧调用链。

原始设计明确提出：LangGraph 是唯一编排器；Pydantic 定义状态与产物协议；模型、工具、外部 Agent 分别经过独立 Adapter；支持 Checkpoint、审查、局部返工、人工介入和离线评估。这些是实现输入，不是项目中已经存在的能力。

## 当前调用链

```text
CLI / Agent API
  → RunManager(run_id / thread_id)
  → LangGraph Root Graph
  → Normalizer
  → Planner
  → dependency-aware Worker fan-out / fan-in
  → deterministic Validator
  → Fact / Logic / Constraint Reviewer fan-out / fan-in
  → Judge
     → Synthesizer → END
     → Repair Planner → targeted Workers (max 1 round)
     → Human Gate (interrupt / resume)
     → Fail → END
```

LangGraph 节点只引用逻辑模型。`ModelGateway` 将逻辑模型路由到 Mock 或 OpenAI 兼容 Provider；Worker 把白名单工具 Schema 交给模型，经 `ToolGateway` 校验并执行 tool call，再把受控摘要作为 tool message 送回模型。Graph State 只保存可序列化合同、参数哈希和受控摘要，不保存 SDK 私有响应和密钥。

## 持久化边界

运行保存拆分为三个可替换边界：LangGraph Checkpoint 保存完整 Graph State；`RunStore` 保存运行索引、状态和哈希指纹；前端 `ConversationStorage` 保存浏览器群聊状态。`RunManager` 通过 `HarnessRuntime` 注入 RunStore，不直接依赖 SQLite。

默认 SQLite 模式分别使用 `var/checkpoints.sqlite` 和 `var/runs.sqlite`。测试与离线评估使用 Memory 模式，避免污染本地持久化数据。相同幂等键通过 RunStore 的原子唯一约束复用原 run_id，请求指纹不一致时拒绝复用。运行租约、心跳和扫描器在单实例环境中续接过期 Checkpoint，并拒绝没有 Checkpoint 的猜测性重放。详细合同与恢复边界见 `docs/persistence.md`。

## 群聊式智能成员分配层

前端群聊不会绕过 Root Graph 直接调用模型。每条群任务使用 `AgentAssignment` 描述分配策略：

```text
群聊消息 / @成员 / 成员选择
  → TaskRequest.assignment
     - mode: auto | direct | cross_functional
     - assignees: 可指派 Agent ID
     - functions_by_agent: 本轮成员到功能标签的映射
     - cross_review: 是否三维交叉复核
  → Planner 生成有界 Plan
  → Assignment Policy 校验并绑定每个 Subtask.worker_role
  → Worker 并发执行，Artifact.producer 记录实际负责人
  → Reviewer 按策略分发 1 或 3 个复核维度
  → Judge / Human Gate / Synthesizer
  → 前端按结构化记录还原为群成员消息
```

可指派池由 `worker-01` 至 `worker-04` 组成，四个成员的模型、工具权限、预算、Schema 和复核配置完全相同。成员编号只用于追踪并发执行槽，永久专长已从 Agent 配置中移除。

`direct` 按用户给定成员顺序绑定任务；`cross_functional` 先从子任务标题和描述识别分析、研究、执行或风险功能，再从本轮声明该功能的同构成员中选择负载最低者。`auto` 在提供功能映射时复用相同路由，否则只做负载均衡。每个子任务把结果写入 `assigned_function`，因此回放时可以区分“谁执行”和“本轮以什么功能执行”。所有 assignee 必须来自配置中 `assignable: true` 的成员池，未知成员或指向未选成员的功能映射会在执行前失败。

## 初始问题分级

| 优先级 | 问题 | 位置 | 影响 | 本轮处理 |
|---|---|---|---|---|
| P0 | 无代码、入口或依赖，项目不可运行 | 项目根目录 | 所有验收项均无法验证 | 已建立可运行 Python 包、CLI 和 API |
| P1 | 无结构化数据合同 | 仅 `agent.md` 文字描述 | 状态不可验证、不可回放 | 已建立 Pydantic 合同和 JSON Schema 导出 |
| P1 | 无评估 baseline / bad case | 项目根目录 | 无法证明后续改动收益 | 已记录零能力 baseline 和固定评估集 |
| P1 | 无 Checkpoint 和人工恢复 | 项目根目录 | 长任务失败后不可恢复 | 已实现内存/SQLite Checkpoint 与 interrupt/resume |
| P1 | 无模型与工具安全边界 | 项目根目录 | 密钥、权限、重试可能混用 | 已拆分 Model/Tool Gateway 并实现基础校验 |
| P2 | Prompt 无文件化版本 | `agent.md` | 结果漂移无法定位 | 已建立 Prompt Registry 和版本元数据 |
| P2 | 无 Trace 和脱敏日志 | 项目根目录 | bad case 难定位 | 已建立 JSON 节点日志和敏感字段脱敏 |
| P2 | 无测试 | 项目根目录 | 改动不可回归 | 已覆盖配置、合同、网关、工具、完整工作流 |

## 仍需确认

- 实际模型供应商、模型名、价格与数据驻留要求；
- 真实业务工具及其审批人、幂等规则和允许域名；
- 面向用户的任务类型和质量评分标准；
- 生产持久化选 PostgreSQL、托管 LangGraph 服务或其他后端；
- API 的身份系统、队列、限流和多租户边界。

## 评估解释边界

当前 `framework-002` 报告使用 10 条确定性的 Mock 契约用例，只验证图拓扑、Schema、并行归并、安全中断和运行稳定性，并统计解析错误、P95、Token、工具调用及 bad case。报告中的 `success_rate` 不能解释为真实模型的回答质量提升，也不能用于比较单 Agent 与多 Agent 效果；接入真实 Provider、质量评分器和人工标注集后，才建立可用于效果比较的 baseline。
